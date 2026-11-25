# 地址人工复核+视觉校验后端 实现计划

> **给执行智能体的说明：** 必须使用子技能 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐个实现本计划。步骤用`- [ ]`勾选框语法追踪进度。

**目标：** 给`videos.location_id`加人工确认+锁定机制，新增`location_group`
（同一地点的多个GPS簇合并）概念，自己实现一套SIFT视觉比对（借鉴
`extern/AD_pipeline_1`的算法思路，不import它的代码）产出自动信号，
LLM做旁路影子判断并记录跟人工的一致率，全程加变更历史和白名单token
鉴权。只做后端（数据模型+服务+API），不含前端。

**架构：** 新增6张表（`location_groups`、`location_review_items`、
`location_change_history`、`operator_api_tokens`，外加`locations`/
`videos`/`operators`各加几列），SQL以`.sql`文件交给用户自己执行。业务
逻辑按关注点拆成几个独立的`app/services/*.py`文件：视觉比对（完全
自包含，不依赖任何其它仓库）、复核队列（创建候选、记录人工决策、
加锁、写历史）、LLM影子判断（独立于复核队列的决策流程，只负责产出
一条记录供以后算一致率）、鉴权（token校验依赖）。API全部挂
`/location-review`前缀，统一走新鉴权依赖。

**技术栈：** FastAPI + SQLAlchemy(异步) + asyncpg（跟现有后端一致）；
视觉比对新增`opencv-python-headless`依赖（SIFT自己实现，不可避免要用
OpenCV），截图提取用`ffmpeg`子进程（跟现有`video_metadata.py`调
`ffprobe`走`subprocess`同一惯例）；LLM走现有`app/llm_agent/client.py`
的`AsyncAnthropic`客户端。

**设计文档：** `docs/superpowers/specs/2026-11-25-location-review-design.md`

## 全局约束

- 数据库schema改动以`.sql`文件交给用户执行，绝不自己执行写操作——
  跟仓库里`add_pipeline_artifacts.sql`同一惯例。
- 没有pytest——验证方式是语法/import检查（`python3 -c "import ast; ...”`
  或`python3 -c "import app.xxx"`）+手动curl跑起来的服务，不单方面
  引入pytest依赖。
- 视觉比对不import`extern/AD_pipeline_1`的任何模块、不碰`sys.path`——
  只借鉴算法思路（SIFT+RANSAC+inlier阈值），在`aerial_backend`里完全
  自包含实现（2026-11-25用户明确要求：自己写，不嵌套引用别的仓库）。
- 视觉比对的自动接受阈值（内点数≥12、内点率≥0.35，`match_policy.
  with_auto_acceptance`）原样复用，不修改、不重新标定。
- `operators.role`字段是现有角色语义（pilot/annotator等），复核权限
  用新的独立布尔列，不覆用`role`。
- token只存哈希（如`hashlib.sha256`），明文只在生成那一刻打印一次，
  不落库、不写日志。
- `_assign_city_site`现有的SRT GPS聚类算法本身不改，只加"跳过已锁定
  视频"这一条新判断。

## Review Focus（容易漏测的点）

- 对一个`location_confirmed=true`的视频再次尝试写入复核决策——必须
  显式拒绝（提示"已锁定，需要先解锁"），不能静默覆盖人工已确认的结果。
- token被撤销（`revoked_at`非空）后仍然用它请求——必须401，不能因为
  token在表里查得到就当作有效。
- 视频对应的本地文件无法提取出有效帧（文件损坏/路径不存在）——创建
  复核候选时不能因为截图失败而整体报错崩溃，应该把该候选标记为
  "截图失败"状态，视觉比对结果留空，照样允许人工凭其他佐证做决策。
- LLM影子判断调用失败（网络/API错误/模型拒绝）——不能影响复核候选的
  创建和人工决策流程，`llm_verdict`留空、记一条日志即可，人工决策不
  依赖LLM结果。
- 合并两个`locations`，其中一个已经属于另一个`location_group`——必须
  显式报错（"已属于另一个分组"），不能静默把它从旧分组里挪走。

---

## 文件结构

**新增SQL迁移（交给用户执行）：**
- `aerial_backend/add_location_review_schema.sql`：所有新表+新列。

**修改：**
- `aerial_backend/app/models.py`：新增`LocationGroup`、
  `LocationReviewItem`、`LocationChangeHistory`、`OperatorApiToken`
  四个ORM类；`Operator`加`can_review_location`列；`Location`加
  `location_group_id`列；`Video`加`location_confirmed`/
  `location_confirmed_by`/`location_confirmed_at`三列。
- `aerial_backend/app/services/scanner.py`：`_assign_city_site`开头
  加"已锁定则跳过"的判断。
- `aerial_backend/app/main.py`：挂新router。

**新增：**
- `aerial_backend/app/services/location_visual_match.py`：自己实现的
  截图提取(ffmpeg子进程)+SIFT比对，不import`extern/AD_pipeline_1`。
- `aerial_backend/app/services/location_auth.py`：token生成/校验+
  FastAPI鉴权依赖。
- `aerial_backend/scripts/issue_location_reviewer_token.py`：CLI，
  给指定operator生成一个token，打印一次。
- `aerial_backend/app/services/location_history.py`：变更历史写入
  的小工具函数，供其它service调用。
- `aerial_backend/app/services/location_review.py`：复核队列核心
  逻辑（创建候选、记录人工决策、加锁、合并location_group）。
- `aerial_backend/app/services/location_llm_judge.py`：LLM影子判断+
  一致率计算。
- `aerial_backend/app/api/routers/location_review.py`：对外API。

---

## Task 1: 数据库schema + ORM模型

**Files:**
- Create: `aerial_backend/add_location_review_schema.sql`
- Modify: `aerial_backend/app/models.py`

**Interfaces:**
- Produces：`LocationGroup`（`id`, `name`, `confirmed_by`, `confirmed_at`,
  `created_at`）、`LocationReviewItem`（`id`, `video_sha256`,
  `candidate_location_id`(可空), `screenshot_path`(可空),
  `visual_match_result`(JSONB, 可空), `llm_verdict`(JSONB, 可空),
  `llm_verdict_at`(可空), `human_decision`(`str|None`,
  取值`same_location`/`different_location`/`new_location`),
  `decided_by`(可空FK operators.id), `decided_at`(可空), `status`
  (`str`, 取值`pending`/`decided`/`screenshot_failed`), `created_at`）、
  `LocationChangeHistory`（`id`, `entity_type`(str, 'video'|'location'|
  'location_group'), `entity_id`(bigint), `field_name`(str),
  `old_value`(text,可空), `new_value`(text,可空), `changed_by`(可空FK
  operators.id), `changed_at`, `reason`(可空)）、`OperatorApiToken`
  （`id`, `operator_id`FK, `token_hash`(unique), `created_at`,
  `revoked_at`可空）。`Location.location_group_id`（可空FK）、
  `Video.location_confirmed`(bool,默认false)/`location_confirmed_by`
  (可空FK)/`location_confirmed_at`(可空)、`Operator.
  can_review_location`(bool,默认false)。

- [ ] **Step 1: 写SQL文件**

`aerial_backend/add_location_review_schema.sql`，内容包括：

```sql
SET search_path TO aerial, public;

CREATE TABLE IF NOT EXISTS location_groups (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL,
    confirmed_by integer REFERENCES operators(id),
    confirmed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE locations
    ADD COLUMN IF NOT EXISTS location_group_id bigint REFERENCES location_groups(id);

ALTER TABLE videos
    ADD COLUMN IF NOT EXISTS location_confirmed boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS location_confirmed_by integer REFERENCES operators(id),
    ADD COLUMN IF NOT EXISTS location_confirmed_at timestamptz;

ALTER TABLE operators
    ADD COLUMN IF NOT EXISTS can_review_location boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS operator_api_tokens (
    id          bigserial PRIMARY KEY,
    operator_id integer NOT NULL REFERENCES operators(id),
    token_hash  text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE TABLE IF NOT EXISTS location_review_items (
    id                     bigserial PRIMARY KEY,
    video_sha256           bytea NOT NULL REFERENCES videos(sha256),
    candidate_location_id  bigint REFERENCES locations(id),
    screenshot_path        text,
    visual_match_result    jsonb,
    llm_verdict            jsonb,
    llm_verdict_at         timestamptz,
    human_decision         text CHECK (human_decision IN ('same_location', 'different_location', 'new_location')),
    decided_by             integer REFERENCES operators(id),
    decided_at             timestamptz,
    status                 text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'decided', 'screenshot_failed')),
    created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS location_change_history (
    id          bigserial PRIMARY KEY,
    entity_type text NOT NULL CHECK (entity_type IN ('video', 'location', 'location_group')),
    entity_id   bigint NOT NULL,
    field_name  text NOT NULL,
    old_value   text,
    new_value   text,
    changed_by  integer REFERENCES operators(id),
    changed_at  timestamptz NOT NULL DEFAULT now(),
    reason      text
);
```

- [ ] **Step 2: 在`app/models.py`里加对应ORM类**（照搬上面SQL的列名/
  类型，风格跟`PipelineArtifact`类一致：bytea→`LargeBinary`，
  jsonb→`JSONB`，text[CHECK]→`Mapped[str]`不在ORM层重复CHECK约束）

- [ ] **Step 3: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/models.py').read())"`
Expected: 无输出（语法通过）

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/add_location_review_schema.sql aerial_backend/app/models.py
git commit -m "feat: 地址人工复核+视觉校验 数据库schema"
```

---

## Task 2: 鉴权——token生成/校验

**Files:**
- Create: `aerial_backend/app/services/location_auth.py`
- Create: `aerial_backend/scripts/issue_location_reviewer_token.py`
- Test: 手动curl（无pytest）

**Interfaces:**
- Consumes：`app.models.Operator`, `app.models.OperatorApiToken`,
  `app.db.get_session`
- Produces：`hash_token(raw: str) -> str`、
  `issue_token(session, operator_id: int) -> str`（返回明文token，只
  在调用的这一刻存在，DB里只存`hash_token`后的值）、
  `require_location_reviewer(authorization: str | None = Header(None),
  session: AsyncSession = Depends(get_session)) -> Operator`（FastAPI
  依赖，校验失败抛`HTTPException(401, ...)`）

- [ ] **Step 1: 实现`location_auth.py`**

`hash_token`用`hashlib.sha256(raw.encode()).hexdigest()`；`issue_token`
用`secrets.token_urlsafe(32)`生成明文、写入`token_hash`；
`require_location_reviewer`解析`Authorization: Bearer <token>`，查
`OperatorApiToken.token_hash == hash_token(token)`且`revoked_at IS
NULL`，再确认关联的`Operator.can_review_location`为true，否则401
（统一用一个错误信息，不区分"token不存在"/"token被撤销"/"无权限"三种
原因，避免给攻击者分辨信息）。

- [ ] **Step 2: 实现`issue_location_reviewer_token.py`**

CLI：`--operator-username <username>`，查到operator后调
`issue_token`、commit、打印明文token+一行提醒"这是唯一一次能看到明文
的机会"。

- [ ] **Step 3: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/services/location_auth.py').read()); ast.parse(open('scripts/issue_location_reviewer_token.py').read())"`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/app/services/location_auth.py aerial_backend/scripts/issue_location_reviewer_token.py
git commit -m "feat: 地址复核白名单token鉴权"
```

---

## Task 3: 视觉比对——自己实现，不import别的仓库

**Files:**
- Create: `aerial_backend/app/services/location_visual_match.py`
- Modify: `aerial_backend/pyproject.toml`（加`opencv-python-headless`
  依赖——SIFT是OpenCV的算法，这个依赖绕不开）

**Interfaces:**
- Produces：
  - `extract_frame(video_path: str, output_path: str) -> None`（用
    `subprocess`调`ffmpeg -y -i <video_path> -vframes 1 -q:v 2
    <output_path>`提取第一帧，跟`video_metadata.py`调`ffprobe`走
    `subprocess`是同一惯例；`ffmpeg`非0退出码或`output_path`最终
    不存在都抛`ValueError(f"截图提取失败: {stderr}")`）
  - `compare_frames(frame_a_path: str, frame_b_path: str) -> dict`
    （返回`{"matched": bool, "inliers": int, "inlier_ratio": float,
    "auto_accepted": bool, "auto_reason": str}`；内部自己提取SIFT
    特征+knn匹配+RANSAC估计单应矩阵，任何一步失败/特征点不足都返回
    `{"matched": False, "inliers": 0, "inlier_ratio": 0.0,
    "auto_accepted": False, "auto_reason": "无法估计有效对齐矩阵"}`
    而不是向上抛异常）

- [ ] **Step 1: 实现`extract_frame`**

- [ ] **Step 2: 实现`compare_frames`**

借鉴（不import）`point_matching.py`的`_image_homography`思路：
`cv2.imread`读两张图转灰度→`cv2.SIFT_create()`提取关键点+描述子→
`cv2.BFMatcher(cv2.NORM_L2).knnMatch(desc_b, desc_a, k=2)`+0.8倍比值
筛选好的匹配→少于4对匹配直接返回"无法估计"→
`cv2.findHomography(src, dst, cv2.RANSAC, 4.0, maxIters=3000,
confidence=.995)`拿`H`和`mask`→`H`为`None`或矩阵不可逆也返回"无法
估计"→`inliers = int(mask.sum())`，`inlier_ratio = inliers /
len(good_matches)`→按"内点数≥12 且 内点率≥0.35"判`auto_accepted`
（数值跟`match_policy.with_auto_acceptance`一致，逻辑自己写一个小
函数，不import那个文件）。

- [ ] **Step 3: 验证**

Run: `cd aerial_backend && python3 -c "
from app.services.location_visual_match import extract_frame, compare_frames
print('import ok')
"`
Expected: 打印`import ok`（纯语法/依赖检查，不要求真的跑一次提取，
因为需要真实视频文件和`ffmpeg`二进制）

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/app/services/location_visual_match.py aerial_backend/pyproject.toml
git commit -m "feat: 地址视觉校验(SIFT+RANSAC)，自己实现不依赖AD_pipeline_1"
```

---

## Task 4: 变更历史 + 聚类跳过锁定视频

**Files:**
- Create: `aerial_backend/app/services/location_history.py`
- Modify: `aerial_backend/app/services/scanner.py:113-135`
  （`_assign_city_site`开头）

**Interfaces:**
- Produces：`record_change(session, *, entity_type: str, entity_id: int,
  field_name: str, old_value: str | None, new_value: str | None,
  changed_by: int | None, reason: str | None = None) -> None`
  （只`session.add`一行`LocationChangeHistory`，不自己commit——调用方
  决定事务边界）

- [ ] **Step 1: 实现`location_history.py`的`record_change`**

- [ ] **Step 2: 修改`_assign_city_site`**，在现有`if video.collect_lon
  is None or video.collect_lat is None: return`之前加
  `if video.location_confirmed: return`

- [ ] **Step 3: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/services/location_history.py').read()); ast.parse(open('app/services/scanner.py').read())"`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/app/services/location_history.py aerial_backend/app/services/scanner.py
git commit -m "feat: 地址变更历史记录 + 聚类跳过人工已锁定视频"
```

---

## Task 5: 复核队列核心逻辑

**Files:**
- Create: `aerial_backend/app/services/location_review.py`

**Interfaces:**
- Consumes：Task 1的四个新ORM类、Task 3的`compare_frames`/
  `extract_frame`、Task 4的`record_change`
- Produces：
  - `create_review_item(session, video_sha256: bytes,
    candidate_location_id: int | None, video_path: str,
    candidate_frame_path: str | None) -> LocationReviewItem`
    （截图/比对失败时`status="screenshot_failed"`而不是抛异常，成功
    则`status="pending"`）
  - `apply_human_decision(session, item_id: int, decision: str,
    operator_id: int, *, target_location_id: int | None = None,
    reason: str | None = None) -> LocationReviewItem`（`decision`取值
    同`human_decision`枚举；已经`location_confirmed=true`的视频直接
    抛`ValueError("视频地址已锁定，需先解锁")`；成功后设置
    `video.location_id`(若`decision != "different_location"`)、
    `video.location_confirmed=True`、`location_confirmed_by`/
    `_at`，并调`record_change`记两条历史：location_id变更+锁定状态
    变更）
  - `merge_location_groups(session, location_ids: list[int],
    group_name: str, operator_id: int) -> LocationGroup`（任一
    `location_id`已属于别的`location_group_id`则抛
    `ValueError("已属于另一个分组")`；否则新建`LocationGroup`、把
    这些`locations.location_group_id`指过去、`record_change`记录）

- [ ] **Step 1: 实现`create_review_item`**

- [ ] **Step 2: 实现`apply_human_decision`**

- [ ] **Step 3: 实现`merge_location_groups`**

- [ ] **Step 4: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/services/location_review.py').read())"`
Expected: 无输出

- [ ] **Step 5: Commit**

```bash
git add aerial_backend/app/services/location_review.py
git commit -m "feat: 地址复核队列核心逻辑(创建候选/人工决策/分组合并)"
```

---

## Task 6: LLM影子判断 + 一致率

**Files:**
- Create: `aerial_backend/app/services/location_llm_judge.py`

**Interfaces:**
- Consumes：`app.llm_agent.client.get_client`/`get_model`，
  `LocationReviewItem`
- Produces：
  - `judge_async(item: LocationReviewItem, frame_a_path: str,
    frame_b_path: str) -> dict`（返回`{"same_location": bool,
    "confidence": float, "reasoning": str}`；调用Claude失败/超时/
    解析失败统一捕获，返回`{"same_location": None, "confidence": 0.0,
    "reasoning": f"调用失败: {原始异常}"}`，不向上抛异常——不能影响
    复核队列主流程）
  - `compute_agreement_rate(session, *, exclude_corrected: bool =
    True) -> dict`（统计所有`status="decided"`且`llm_verdict`不为空
    的条目，`llm_verdict.same_location`跟`human_decision=="same_
    location"`是否一致，返回`{"total": int, "agreed": int,
    "disagreed": int, "ready_for_automation": bool}`；
    `ready_for_automation`要求`total>=50`且`disagreed==0`；
    `exclude_corrected`为真时跳过"该条历史决策后来被人工用新的决策
    覆盖过"的条目——判断方式是同一`video_sha256`+相同
    `candidate_location_id`存在不止一条`location_change_history`
    记录）

- [ ] **Step 1: 实现`judge_async`**，用
  `get_client().messages.create(model=get_model(), max_tokens=512,
  messages=[{"role": "user", "content": [
  {"type": "image", "source": {"type": "base64", "media_type":
  "image/jpeg", "data": <frame_a的base64>}},
  {"type": "image", "source": {"type": "base64", "media_type":
  "image/jpeg", "data": <frame_b的base64>}},
  {"type": "text", "text": "..."}]}])`，提示词明确要求返回JSON（用
  现有`app/llm_agent/runner.py`里解析JSON响应的同一套做法，不要重新
  发明一套解析逻辑）

- [ ] **Step 2: 实现`compute_agreement_rate`**

- [ ] **Step 3: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/services/location_llm_judge.py').read())"`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/app/services/location_llm_judge.py
git commit -m "feat: LLM地址判断影子模式 + 人工一致率统计"
```

---

## Task 7: API接口 + 挂载

**Files:**
- Create: `aerial_backend/app/api/routers/location_review.py`
- Modify: `aerial_backend/app/main.py`

**Interfaces:**
- Consumes：Task 2的`require_location_reviewer`，Task 5/6的所有函数

- [ ] **Step 1: 实现路由**（全部`Depends(require_location_reviewer)`）

```
GET  /location-review/queue?status=pending          列表
GET  /location-review/queue/{item_id}                详情
POST /location-review/queue/{item_id}/decide         人工决策
POST /location-review/groups/merge                    合并location_group
GET  /location-review/agreement-rate                  LLM一致率统计
```

`decide`请求体（pydantic `BaseModel`）：`decision: str`,
`target_location_id: int | None = None`, `reason: str | None = None`；
捕获`apply_human_decision`/`merge_location_groups`抛出的`ValueError`
转成`HTTPException(409, str(exc))`（409不是400——这是状态冲突，不是
输入格式错误）。

- [ ] **Step 2: 在`main.py`挂router**：`from app.api.routers import
  ..., location_review` + `app.include_router(location_review.router)`

- [ ] **Step 3: 验证**

本地起服务后手动curl：
```bash
curl -X GET "http://localhost:8088/location-review/queue" -H "Authorization: Bearer <用Task2脚本生成的token>"
```
Expected: 200，返回空列表`[]`（数据库里还没有任何复核候选）；不带
Authorization header时返回401

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/app/api/routers/location_review.py aerial_backend/app/main.py
git commit -m "feat: 地址复核API接口"
```

---

## 自我检查记录

- 规格覆盖：数据模型(Task1)、鉴权(Task2)、视觉比对(Task3)、锁定+历史
  (Task4)、复核队列(Task5)、LLM影子判断(Task6)、API(Task7)——设计
  文档里"已确认的设计决策"8条全部有对应任务落地；"前端"/"LLM自动切换
  开关"按设计文档"非目标"明确排除，未纳入任务。
- Review Focus的5条均已各自落在对应任务的某个Step里（见Task5 Step2的
  锁定校验、Task2 Step1的revoked_at校验、Task3 Step2/3对失败情况的
  处理、Task6 Step1对LLM调用失败的降级、Task5 Step3对跨组合并的报错）。
