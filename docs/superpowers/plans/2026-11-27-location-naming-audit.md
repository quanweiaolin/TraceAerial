# Location Naming Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给每个location_id重新从视频`storage_path`统计飞手当年打的原始site文本，自动发现"同一个location_id下名字拼法不一致"和"不同location_id其实是同名地点"两类问题，生成待人工确认的提示，而不是自动改任何数据。

**Architecture:** 新增一张`location_naming_alerts`表+一个新服务模块(纯函数做判定逻辑+async函数做DB聚合/写入)+两个新API(列提示、确认/驳回)+一个新前端页面，另外在`scanner.py`现有的`_assign_city_site`之后挂一个钩子，让新视频持续维护这份统计；一次性backfill脚本覆盖现有42个location。

**Tech Stack:** FastAPI + SQLAlchemy(async) + Postgres(JSONB) 后端；React+TS+Vite+Vitest+MSW 前端，复用`useApiQuery`。

**Spec:** docs/superpowers/specs/2026-11-27-location-naming-audit-design.md

## Global Constraints

- 绝不改NAS上的文件/文件夹名字——只写`location_naming_alerts`表和`Location.site`列，且后者只在人工点确认之后才写。
- 绝不自动合并location_id、绝不自动改名——所有写`Location.site`的动作都来自人工在前端点确认，没有任何后台自动触发的改名/合并。
- 这一版不接飞书通知——不写任何通知/webhook代码。
- 不改`backfill_address_clustering.py`/`_assign_city_site`既有的聚类判定逻辑本身，只在其之后追加一次审计调用。
- 原始site/city必须用`session_naming.find_session_match`从`Video.storage_path`重新解析，不读`Location.site`/`Location.city`两列。解析不出来的视频跳过，不参与任何统计，不报错。
- 后端这个项目没有pytest基础设施，约定是`python3 -c "import ast; ast.parse(...)"`语法检查；本计划里纯逻辑函数(不碰DB/session)例外——用不依赖pytest的、基于`assert`的小脚本做真正的红绿验证，因为这部分恰好是builder-check揪出bug的那块核心判定逻辑，值得真测试覆盖。
- `location_naming_alerts`的插入要对照`scanner.py:313-320`现有的并发竞态处理模式(`begin_nested()`+单独catch`IntegrityError`)。
- `Location.site`的`UNIQUE(city, site)`约束撞车要返回400，不能让`IntegrityError`冒成500。

## Review Focus

- 一个location_id下全部成员视频原始site文本完全一样，不生成任何提示（Task 1测试覆盖）。
- 只有1条成员视频的location_id不该被当成"漂移"（Task 1测试覆盖）。
- confirm/dismiss一条提示后，投票数据没变，下一次检测不应该重新生成同样的pending提示（Task 1测试覆盖，这是builder-check发现的原设计漏洞）。
- 一组location已经通过Groups合并成同一个`location_group_id`后，不该再生成针对这组的`merge_candidate`提示（Task 1测试覆盖）。
- 两个不同city下出现一样的原始site文本，不该被当成合并候选（Task 1测试覆盖，city分组用重新解析的值）。

---

### Task 1: 核心判定逻辑(纯函数+真测试)

**Files:**
- Create: `aerial_backend/app/services/location_naming_audit.py`
- Create: `aerial_backend/scripts/test_location_naming_audit_logic.py`（不是pytest，是一个独立可执行的assert测试脚本，专门测这个模块里不碰DB的纯函数——文件名加`test_`前缀只是让"这是测试"一目了然，不会被当成生产脚本误跑）

**Interfaces:**
- Produces：
  - `tally_raw_site_votes(video_rows: list[tuple[int, str]]) -> dict[str, dict[int, dict[str, int]]]`：输入`(location_id, storage_path)`列表，用`find_session_match`重新解析每条的city/site，解析不出来的跳过；返回`{city: {location_id: {raw_site: 次数}}}`三层字典。
  - `compute_votes_fingerprint(raw_site_votes: dict[str, int]) -> str`：对单个location_id的`{raw_site: 次数}`字典按key排序后序列化(`json.dumps(..., sort_keys=True)`)再取md5 hexdigest，保证同样内容无论字典插入顺序如何都得到同一个fingerprint。
  - `AlertCandidate`：一个小`dataclass`，字段`alert_type: Literal["name_drift", "merge_candidate"]`、`city: str`、`location_ids: list[int]`、`raw_site_votes: dict`、`suggested_value: str`。
  - `build_alert_candidates(city_votes: dict[int, dict[str, int]]) -> list[AlertCandidate]`：输入单个city下`{location_id: {raw_site: 次数}}`（即`tally_raw_site_votes`结果三层字典里某个city对应的那一层），输出该city下全部`name_drift`(某location_id的字典有超过1个key，`suggested_value`取次数最高的key，`raw_site_votes`放这个location_id自己那份`{raw_site: 次数}`)和`merge_candidate`(某个raw_site字符串出现在超过1个location_id里，`location_ids`是涉及的全部location_id，`suggested_value`是那个共享的raw_site，`raw_site_votes`放这几个location_id各自的票数`{raw_site: {location_id: 次数}}`——跟`name_drift`的`raw_site_votes`形状不同，分别对应设计文档里描述的两种展示需求）候选。只接收单个city的votes，不接收`tally_raw_site_votes`三层字典的全集——结构上保证跨city永远不会被拿来比较，不需要额外的运行时检查。
  - `is_duplicate_fingerprint(fingerprint: str, existing_fingerprints: set[str]) -> bool`：`detect_naming_alerts`（Task 2）查出"这个`(alert_type, location_ids)`组合下已有的全部`votes_fingerprint`（不限status）"之后，用这个函数判断新算出的fingerprint要不要跳过——纯函数，把"去重不分status"这条设计决定单独拎出来，才能在不碰DB的前提下测。
  - `is_already_grouped(location_ids: list[int], group_id_by_location: dict[int, int | None]) -> bool`：`detect_naming_alerts`（Task 2）查出这几个location_id各自当前的`location_group_id`之后，用这个函数判断是否全部非空且相等——是则这个`merge_candidate`候选要被抑制，不生成。

- [ ] **Step 1: 写失败测试**

```python
# aerial_backend/scripts/test_location_naming_audit_logic.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.location_naming_audit import (
    tally_raw_site_votes, compute_votes_fingerprint, build_alert_candidates,
    is_duplicate_fingerprint, is_already_grouped,
)


def test_tally_groups_by_reparsed_city_and_site():
    rows = [
        (22, "/mnt/nas/.../dongguan_guanfogaosuchuzadao_m3e_300m_lixiupeng_20230217/DJI_.../a.MP4"),
        (23, "/mnt/nas/.../dongguan_guanfogaosuchuzadao_m3e_300m_lixiupeng_20230217/DJI_.../b.MP4"),
        (99, "/mnt/nas/.../不符合命名规范的历史路径/c.MP4"),  # 解析不出来，应跳过
    ]
    result = tally_raw_site_votes(rows)
    assert result == {"dongguan": {22: {"guanfogaosuchuzadao": 1}, 23: {"guanfogaosuchuzadao": 1}}}


def test_tally_keeps_different_cities_separate_even_with_same_site_text():
    rows = [
        (22, "/mnt/nas/.../dongguan_guanfogaosuchuzadao_m3e_300m_lixiupeng_20230217/DJI_.../a.MP4"),
        (50, "/mnt/nas/.../changchun_guanfogaosuchuzadao_m3e_200m_wangziyu_20260912/DJI_.../a.MP4"),
    ]
    result = tally_raw_site_votes(rows)
    # 两个city各自只看到自己的一个location_id，互不污染——即使site文本撞了
    assert result == {
        "dongguan": {22: {"guanfogaosuchuzadao": 1}},
        "changchun": {50: {"guanfogaosuchuzadao": 1}},
    }


def test_duplicate_fingerprint_detected_regardless_of_existing_status():
    # 去重不看status——这是builder-check发现的原漏洞：旧设计只挡pending，
    # 导致confirm/dismiss过的提示在下次扫描(投票数据没变)时被原样复活
    assert is_duplicate_fingerprint("abc123", {"abc123"}) is True
    assert is_duplicate_fingerprint("abc123", set()) is False


def test_merge_candidate_suppressed_when_already_grouped():
    assert is_already_grouped([22, 23], {22: 5, 23: 5}) is True
    assert is_already_grouped([22, 23], {22: 5, 23: None}) is False
    assert is_already_grouped([22, 23], {22: 5, 23: 6}) is False


def test_clean_cluster_no_alert():
    candidates = build_alert_candidates({22: {"guanfogaosuchuzadao": 50}})
    assert candidates == []


def test_single_video_location_no_drift_alert():
    candidates = build_alert_candidates({23: {"guanfogaosuchuzadao": 1}})
    assert candidates == []


def test_name_drift_suggests_majority():
    candidates = build_alert_candidates({22: {"guanfogaosuchuzadao": 48, "guanfugaosuchuzadao": 2}})
    assert len(candidates) == 1
    assert candidates[0].alert_type == "name_drift"
    assert candidates[0].suggested_value == "guanfogaosuchuzadao"


def test_merge_candidate_across_location_ids():
    candidates = build_alert_candidates({
        22: {"guanfogaosuchuzadao": 50},
        23: {"guanfogaosuchuzadao": 1},
    })
    assert len(candidates) == 1
    assert candidates[0].alert_type == "merge_candidate"
    assert set(candidates[0].location_ids) == {22, 23}


def test_fingerprint_stable_regardless_of_key_order():
    a = compute_votes_fingerprint({"guanfogaosuchuzadao": 48, "guanfugaosuchuzadao": 2})
    b = compute_votes_fingerprint({"guanfugaosuchuzadao": 2, "guanfogaosuchuzadao": 48})
    assert a == b


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd aerial_backend && uv run python scripts/test_location_naming_audit_logic.py`
Expected: `ModuleNotFoundError` 或 `ImportError`（`location_naming_audit.py`还不存在）

- [ ] **Step 3: 实现`location_naming_audit.py`里的`tally_raw_site_votes`/`compute_votes_fingerprint`/`AlertCandidate`/`build_alert_candidates`/`is_duplicate_fingerprint`/`is_already_grouped`**

`tally_raw_site_votes`内部对每条`storage_path`调`session_naming.find_session_match`，拿`match.group("city")`/`match.group("site")`；`build_alert_candidates`按"目标"里描述的两条规则（某location_id的key数>1 → drift；某个key出现在>1个location_id → merge_candidate）生成候选，用`dataclass`承载；`is_duplicate_fingerprint`/`is_already_grouped`是两个几行代码的纯判断函数，供Task 2的`detect_naming_alerts`调用。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd aerial_backend && uv run python scripts/test_location_naming_audit_logic.py`
Expected: 9行`PASS test_xxx`，无异常

- [ ] **Step 5: Commit**

```bash
git add aerial_backend/app/services/location_naming_audit.py aerial_backend/scripts/test_location_naming_audit_logic.py
git commit -m "feat: 命名审核核心判定逻辑(投票统计/fingerprint/漂移与合并候选判定)"
```

---

### Task 2: 数据模型+DB聚合与写入层

**Files:**
- Modify: `aerial_backend/add_location_review_schema.sql`
- Modify: `aerial_backend/app/models.py`
- Modify: `aerial_backend/app/services/location_naming_audit.py`

**Interfaces:**
- Consumes：Task 1的`tally_raw_site_votes`/`compute_votes_fingerprint`/`build_alert_candidates`/`AlertCandidate`
- Produces：
  - ORM模型`LocationNamingAlert`（见下）。
  - `async def compute_city_votes(session: AsyncSession, city: str) -> dict[int, dict[str, int]]`：查这个city下`location_id`不为空的全部视频的`(location_id, storage_path)`，调`tally_raw_site_votes`取出该city那一层返回（没有就返回`{}`）。
  - `async def detect_naming_alerts(session: AsyncSession, city: str) -> int`：返回这次调用里真正新插入的提示条数（供Task 5的backfill脚本打印统计用，不用额外去对比commit前后的表行数）；调`compute_city_votes`+`build_alert_candidates`拿到候选列表；对每个候选算`votes_fingerprint`；若`alert_type=="merge_candidate"`，查一次涉及的`location_ids`各自当前的`Location.location_group_id`，调Task 1的`is_already_grouped`判断要不要跳过；再查一次这个`(alert_type, location_ids)`组合下已有的全部`votes_fingerprint`（任意status），调Task 1的`is_duplicate_fingerprint`判断要不要跳过；都不跳过则在`begin_nested()`里插入新的`pending`记录，catch`IntegrityError`（并发扫描抢先插入）后忽略。不在这里`commit`——调用方（scanner.py钩子/backfill脚本）决定何时commit。这个函数本身只是把Task 1两个纯函数的判断结果接到真实的DB查询/插入上，不自己重新实现判断逻辑。

- [ ] **Step 1: 在`add_location_review_schema.sql`末尾追加建表语句**

```sql
CREATE TABLE IF NOT EXISTS location_naming_alerts (
    id                 bigserial PRIMARY KEY,
    alert_type         text NOT NULL CHECK (alert_type IN ('name_drift', 'merge_candidate')),
    city               text NOT NULL,
    location_ids       bigint[] NOT NULL,
    raw_site_votes     jsonb NOT NULL,
    votes_fingerprint  text NOT NULL,
    suggested_value    text,
    status             text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'confirmed', 'dismissed')),
    resolved_by        integer REFERENCES operators(id),
    resolved_at        timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_location_naming_alerts_dedupe
    ON location_naming_alerts (alert_type, location_ids, votes_fingerprint);
```

同一个文件顶部已有的部署顺序警告注释（"这份迁移必须在重启跑这份代码的app进程之前执行完"）这次也适用，不用改——新建表不会让现有查询报错，但不先建表的话`LocationNamingAlert`相关的新查询会报`UndefinedTable`。

- [ ] **Step 2: 在`models.py`里加`LocationNamingAlert`**

```python
class LocationNamingAlert(Base):
    __tablename__ = "location_naming_alerts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alert_type: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)
    location_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    raw_site_votes: Mapped[dict] = mapped_column(JSONB)
    votes_fingerprint: Mapped[str] = mapped_column(Text)
    suggested_value: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```
（`ARRAY`从`sqlalchemy`导入，这个文件目前没用过数组列，照SQLAlchemy文档的`from sqlalchemy import ARRAY`加这一个新import。）

- [ ] **Step 3: 跑语法检查**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/models.py').read())"`
Expected: 无输出

- [ ] **Step 4: 实现`compute_city_votes`+`detect_naming_alerts`**

竞态处理参考`app/services/scanner.py:313-320`那段`begin_nested()`+catch`IntegrityError`的写法，`location_ids`数组按升序排好再存，保证同一组合不会因为顺序不同绕过唯一索引。

- [ ] **Step 5: 跑语法检查**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/services/location_naming_audit.py').read())"`
Expected: 无输出

- [ ] **Step 6: Commit**

```bash
git add aerial_backend/add_location_review_schema.sql aerial_backend/app/models.py aerial_backend/app/services/location_naming_audit.py
git commit -m "feat: 命名审核表结构+DB聚合与写入(detect_naming_alerts)"
```

---

### Task 3: API——列提示+确认/驳回

**Files:**
- Modify: `aerial_backend/app/api/routers/location_review.py`

**Interfaces:**
- Consumes：Task 2的`LocationNamingAlert`模型；现有`record_change`（`app.services.location_history`）、`require_location_reviewer`依赖、`Operator`模型
- Produces：
  - `GET /location-review/naming-alerts?status=pending`：返回该状态下的提示列表，每条带`id/alert_type/city/location_ids/raw_site_votes/suggested_value/status/created_at`，外加为每个`location_id`附上当前`Location.site`方便前端展示（查一次`Location`表批量补）。
  - `POST /location-review/naming-alerts/{id}/confirm`，body `{chosen_value?: str}`：
    - `alert_type=="name_drift"`：`chosen_value`非空（trim后非空，否则400），写`Location.site = chosen_value`（对涉及的那个location_id，即`location_ids[0]`）——如果这个新`site`在同city下已经被别的location占用（违反`UNIQUE(city, site)`），捕获该`IntegrityError`，返回400+清楚的中文错误信息，不让它变成500；成功则调`record_change(session, entity_type="location", entity_id=str(location_id), field_name="site", old_value=旧site, new_value=chosen_value, changed_by=operator.id)`。
    - `alert_type=="merge_candidate"`：不碰任何`Location`行，只把alert状态置`confirmed`。
    - 两种情况都设`resolved_by=operator.id`、`resolved_at=now()`，最后`commit`。
  - `POST /location-review/naming-alerts/{id}/dismiss`：状态置`dismissed`+`resolved_by`/`resolved_at`，`commit`。
  - 这个alert不存在/状态已经不是`pending`——返回404/409（已处理过的提示不能重复confirm/dismiss）。

- [ ] **Step 1: 实现三个endpoint**

挂在现有router（`router = APIRouter(prefix="/location-review", ...)`）下，复用`Depends(require_location_reviewer)`同一套鉴权模式，跟文件里其它endpoint风格一致（看`update_group_alias`那段）。

- [ ] **Step 2: 跑语法检查**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/api/routers/location_review.py').read())"`
Expected: 无输出

- [ ] **Step 3: Commit**

```bash
git add aerial_backend/app/api/routers/location_review.py
git commit -m "feat: 命名审核API(列提示+确认/驳回)"
```

---

### Task 4: scanner.py接入

**Files:**
- Modify: `aerial_backend/app/services/scanner.py`

**Interfaces:**
- Consumes：Task 2的`detect_naming_alerts(session, city)`

- [ ] **Step 1: 在`_assign_city_site`给`video.location_id`赋值之后（两处：匹配到已有地址的分支、新建地址的分支）都加一行`await detect_naming_alerts(session, city)`**

两个分支都要加，因为无论"加入已有簇"还是"新建一个簇"，city下的投票分布都可能因为这条新视频而改变（加入已有簇：这个location多了一条可能带新拼写的视频；新建簇：很可能跟city下某个已有簇产生新的merge_candidate）。

- [ ] **Step 2: 跑语法检查**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/services/scanner.py').read())"`
Expected: 无输出

- [ ] **Step 3: Commit**

```bash
git add aerial_backend/app/services/scanner.py
git commit -m "feat: 扫盘流程接入命名审核检测，新视频入库后持续维护投票"
```

---

### Task 5: 一次性backfill脚本

**Files:**
- Create: `aerial_backend/scripts/backfill_location_naming_audit.py`

**Interfaces:**
- Consumes：Task 2的`detect_naming_alerts`

- [ ] **Step 1: 实现脚本**

照`scripts/issue_location_reviewer_token.py`的模式（`sys.path.insert`+`SessionContext`+`argparse`+显式`commit`）：查所有不同的`Location.city`，对每个city调一次`detect_naming_alerts`，累加各次返回值，最后commit一次，打印"扫了N个city，生成了M条新提示"。

docstring开头注明：这个脚本会写数据库（插入`location_naming_alerts`记录，不改`Location`/`Video`任何现有数据），按项目约定交给你自己在生产机上跑，不由我代跑。

- [ ] **Step 2: 跑语法检查**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('scripts/backfill_location_naming_audit.py').read())"`
Expected: 无输出

- [ ] **Step 3: Commit**

```bash
git add aerial_backend/scripts/backfill_location_naming_audit.py
git commit -m "chore: 加命名审核一次性backfill脚本"
```

---

### Task 6: 前端——命名审核页面

**Files:**
- Modify: `location_review_web/src/api/types.ts`
- Create: `location_review_web/src/pages/NamingAuditPage.tsx`
- Create: `location_review_web/src/pages/NamingAuditPage.test.tsx`
- Modify: `location_review_web/src/App.tsx`
- Modify: `location_review_web/src/components/AppShell.tsx`

**Interfaces:**
- Consumes：Task 3的`GET /location-review/naming-alerts?status=pending`、`POST /location-review/naming-alerts/{id}/confirm`、`POST /location-review/naming-alerts/{id}/dismiss`
- Produces：`NamingAlert`类型；路由`/naming-audit`；导航新增一项"命名审核"

- [ ] **Step 1: 写失败测试**

```tsx
// NamingAuditPage.test.tsx 关键用例（结构仿GroupsPage.test.tsx：msw server + currentAlerts可变状态）
test("展示命名漂移提示，确认后从列表消失", async () => {
  currentAlerts = [{
    id: 1, alert_type: "name_drift", city: "dongguan", location_ids: [22],
    raw_site_votes: { guanfogaosuchuzadao: 48, guanfugaosuchuzadao: 2 },
    suggested_value: "guanfogaosuchuzadao", status: "pending", created_at: "2026-11-27T00:00:00Z",
  }];
  // render后应该能看到两个候选名字+各自次数，输入框预填suggested_value
  // 点"确认"后POST /naming-alerts/1/confirm，body包含chosen_value，
  // 成功后该条从列表消失(断言不再findByText)
});

test("展示合并候选提示，点忽略后从列表消失", async () => {
  currentAlerts = [{
    id: 2, alert_type: "merge_candidate", city: "dongguan", location_ids: [22, 23],
    raw_site_votes: { guanfogaosuchuzadao: { "22": 50, "23": 1 } },
    suggested_value: "guanfogaosuchuzadao", status: "pending", created_at: "2026-11-27T00:00:00Z",
  }];
  // 展示涉及的location_ids，有"去Groups页面合并"链接(指向/groups)和"忽略"按钮
  // 点"忽略"后POST /naming-alerts/2/dismiss，该条从列表消失
});

test("没有待处理提示时显示空态", async () => {
  currentAlerts = [];
  // 断言出现"暂无待处理的命名提示"之类文案
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd location_review_web && npx vitest run src/pages/NamingAuditPage.test.tsx`
Expected: FAIL（`NamingAuditPage`模块不存在）

- [ ] **Step 3: 实现`NamingAlert`类型+`NamingAuditPage.tsx`+路由/导航**

`types.ts`加：
```typescript
export interface NamingAlert {
  id: number;
  alert_type: "name_drift" | "merge_candidate";
  city: string;
  location_ids: number[];
  raw_site_votes: Record<string, unknown>;
  suggested_value: string | null;
  status: "pending" | "confirmed" | "dismissed";
  created_at: string;
}
```
`NamingAuditPage`用`useApiQuery<NamingAlert[]>("/location-review/naming-alerts?status=pending")`拉列表；`name_drift`渲染时把`raw_site_votes`当`Record<string, number>`展示每个候选名字+次数、一个预填`suggested_value`的文本输入框+"确认"按钮；`merge_candidate`展示`location_ids`+`suggested_value`+一个指向`/groups`的`<Link>`+"忽略"按钮；两种类型的确认/忽略都调用后再`refetch()`。`App.tsx`加一条`<Route path="naming-audit" element={<NamingAuditPage />} />`；`AppShell.tsx`的`NAV_ITEMS`加一项`{ to: "/naming-audit", label: "命名审核" }`。

- [ ] **Step 4: 跑测试确认通过+全量回归**

Run: `cd location_review_web && npx vitest run && npx tsc -b`
Expected: 全部通过，`tsc`无输出

- [ ] **Step 5: Commit**

```bash
git add location_review_web/src/api/types.ts location_review_web/src/pages/NamingAuditPage.tsx location_review_web/src/pages/NamingAuditPage.test.tsx location_review_web/src/App.tsx location_review_web/src/components/AppShell.tsx
git commit -m "feat: 命名审核页面(展示漂移/合并提示+确认/忽略)"
```
