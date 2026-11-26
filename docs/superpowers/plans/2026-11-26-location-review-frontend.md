# 地址复核前端 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）or superpowers:executing-plans 按任务逐个实现本计划。步骤用`- [ ]`勾选框语法追踪进度。

**Goal:** 给已经上线的地址人工复核后端（8个API）做一套完整的管理界面——复核队列、文件夹审查、地点管理、分组管理、一致率看板，外加3个支撑性的后端只读补丁（GPS距离、候选地点身份内联、文件夹审查接口等）。

**Architecture:** 后端补丁全部是只读/序列化层面的加法（不改聚类算法/不改已验证过的决策状态机），独立新建一个`location_review_web/`React单页应用通过Bearer token消费这套API。前端按"页面=一个React route+一组组件"拆分，API client统一一层处理token注入/401跳转登录。

**Tech Stack:** 后端沿用FastAPI+SQLAlchemy(已有)。前端：Vite + React 18 + TypeScript + react-router；测试用Vitest + React Testing Library + MSW（这是本项目第一个前端，没有既有测试惯例可循，这次引入——用户此前明确表示测试框架"后续再想"，这里只新建**前端**的测试工具链，不触碰后端"没有pytest"的现状）。

**Spec:** `docs/superpowers/specs/2026-11-26-location-review-frontend-design.md`（前端设计）+ `docs/superpowers/specs/2026-11-25-location-review-design.md`（后端设计，前端消费的API来自这里）

## Global Constraints

- 后端补丁不改`scanner.py`/`_assign_city_site`聚类算法，不改`apply_human_decision`/`unlock_video`/`create_new_location`/`merge_location_groups`已有的决策语义——只加新字段/新端点。
- 数据库schema改动（本计划只有`locations.representative_video_sha256`一列）以`.sql`文件追加进`aerial_backend/add_location_review_schema.sql`，交给用户自己执行，绝不自己执行。
- 后端验证方式沿用项目现状（无pytest）：语法/import检查+手动curl。**前端是新代码，这次就用Vitest写真正的单元/组件测试**，不是沿用"无测试"惯例。
- 多帧预留：`screenshot_path`→`screenshot_paths`数组只在序列化层包一层，不改`location_review_items`表结构、不改`extract_frame`。
- 前端鉴权：粘贴token登录，存`localStorage`，无session/无密码系统。
- 复核队列**不做真分页**：后端一次性返回当前status筛选下的全部条目，前端客户端分页（当前量级：几十到几百条）。
- 同一视频的"陪审"item不在后端自动关闭：前端查每条item关联视频的`location_confirmed`字段，已锁定视频名下其余`pending`item前端自己灰显/折叠。

## Review Focus

- 一个`candidate_location_id=None`（"新地点"候选）的复核item——GPS距离/候选身份这些字段天然没有候选方可比，前端渲染这类item时不能因为`candidate_location_id`是null就崩溃，要显示成"无候选，判定为新地点"而不是报错或留白出bug。
- 一个`status="screenshot_failed"`的item——没有`screenshot_paths`可展示，前端要显示"截图提取失败"而不是尝试加载一张不存在的图。
- 一个`visual_match_result`或`llm_verdict`为`null`的item（截图失败或LLM调用失败两种情况都会产生）——前端的指标展示区要能处理"这一项指标缺失"，不能假设两者总是有值。
- token过期/被撤销（后端返回401）——前端任何一次API调用收到401都要统一跳转回登录页、清掉localStorage里的旧token，不能让某个页面卡在一个用不了的半成品状态。
- 文件夹审查面板输入了一个不存在/没有任何视频匹配的路径前缀——必须显示"这个前缀下没有视频"的空态，不能误判成加载中或报错。

---

## 文件结构

**后端（`aerial_backend/`，修改既有文件，不新建service文件）：**
- Modify: `app/models.py`（`Location`加`representative_video_sha256`列）
- Modify: `add_location_review_schema.sql`（追加一条`ALTER TABLE`）
- Modify: `app/services/location_review.py`（`apply_human_decision`的反向回填那段，顺带回填`representative_video_sha256`）
- Modify: `app/api/routers/location_review.py`（`_serialize_review_item`改造+3个新端点+CORS）
- Modify: `app/main.py`（挂`CORSMiddleware`）

**前端（新建`location_review_web/`，repo根目录下，跟`aerial_backend/`同级）：**
- `location_review_web/package.json` / `vite.config.ts` / `tsconfig.json`
- `location_review_web/src/api/client.ts` —— fetch封装，统一注入token、统一处理401
- `location_review_web/src/api/types.ts` —— 跟后端响应形状对应的TS类型
- `location_review_web/src/auth/AuthContext.tsx` —— token的读写（localStorage）+ `useAuth()` hook
- `location_review_web/src/pages/LoginPage.tsx`
- `location_review_web/src/pages/QueuePage.tsx`
- `location_review_web/src/pages/ReviewDetailPage.tsx`
- `location_review_web/src/pages/FolderAuditPage.tsx`
- `location_review_web/src/pages/LocationsPage.tsx`
- `location_review_web/src/pages/GroupsPage.tsx`
- `location_review_web/src/pages/AgreementRatePage.tsx`
- `location_review_web/src/components/AppShell.tsx` —— 侧边栏导航+路由出口
- `location_review_web/src/components/ImageGallery.tsx` —— 渲染`screenshot_paths`数组
- `location_review_web/src/components/MetricsPanel.tsx` —— GPS距离+视觉指标+LLM判断展示（方案A：并排+底部指标条）
- `location_review_web/src/App.tsx` / `src/main.tsx`

---

## Task 1: 后端——`representative_video_sha256`列 + 回填

**Files:**
- Modify: `aerial_backend/add_location_review_schema.sql`
- Modify: `aerial_backend/app/models.py`
- Modify: `aerial_backend/app/services/location_review.py:179-191`（反向回填那段，精确行号以当前文件为准，找`representative_frame_path is None`那个if块）

**Interfaces:**
- Produces：`Location.representative_video_sha256: Mapped[bytes | None]`（`LargeBinary(32)`，`ForeignKey("videos.sha256")`）

- [ ] **Step 1: SQL追加**

在`add_location_review_schema.sql`末尾（`CREATE INDEX`那几行之后）追加：
```sql
ALTER TABLE locations
    ADD COLUMN IF NOT EXISTS representative_video_sha256 bytea REFERENCES videos(sha256);
```

- [ ] **Step 2: ORM加列**，紧挨着`Location.representative_frame_path`那行加
  `representative_video_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32), ForeignKey("videos.sha256"))`

- [ ] **Step 3: 回填逻辑**。在`apply_human_decision`里，现有这段：
  ```python
  if target_location is not None and target_location.representative_frame_path is None:
      record_change(..., field_name="representative_frame_path", ...)
      target_location.representative_frame_path = item.screenshot_path
  ```
  加一个同级的`record_change`+赋值，`field_name="representative_video_sha256"`，
  `old_value=None`，`new_value=sha256_bytes_to_hex(video.sha256)`，
  `target_location.representative_video_sha256 = video.sha256`
  （`video`变量在这个函数里已经存在，不用重新查）

- [ ] **Step 4: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/models.py').read()); ast.parse(open('app/services/location_review.py').read())"`
Expected: 无输出

- [ ] **Step 5: Commit**

```bash
git add aerial_backend/add_location_review_schema.sql aerial_backend/app/models.py aerial_backend/app/services/location_review.py
git commit -m "feat: location加representative_video_sha256，回填GPS比对用的参考视频"
```

---

## Task 2: 后端——复核item序列化加GPS距离+候选身份+视频身份

**Files:**
- Modify: `aerial_backend/app/api/routers/location_review.py`

**Interfaces:**
- Consumes：Task 1的`Location.representative_video_sha256`，`app.services.geo.distance_3d_meters(lon1, lat1, height1, lon2, lat2, height2) -> float`（已存在）
- Produces：`_serialize_review_item`改成`async def _serialize_review_item(session: AsyncSession, item: LocationReviewItem) -> dict`，返回字段在原有基础上新增：
  - `screenshot_paths: list[str]`（原`screenshot_path`包成数组，`None`时是空列表`[]`）
  - `candidate: dict | None`（`candidate_location_id`为`None`时整个字段是`None`；否则`{"id", "city", "site", "representative_frame_path", "group_name"}`，`group_name`没分组时是`None`）
  - `gps_distance_meters: float | None`（候选的`representative_video_sha256`或候选本身为`None`，或任一方GPS字段缺失时是`None`，不是0或报错）
  - `video_filename: str`、`video_city: str | None`、`video_collected_at: datetime | None`、`video_location_confirmed: bool`（从`item.video_sha256`查`Video`拿；`video_location_confirmed`供前端判断"这条item对应的视频是不是已经被别的候选决策锁定了"——同一视频的其它pending item要靠这个字段灰显，见Task5）

- [ ] **Step 1: 改`_serialize_review_item`签名+内部逻辑**

函数体内：
1. `video = await session.get(Video, item.video_sha256)`，算`video_filename = Path(video.storage_path).name`
2. `candidate_location_id`非空时`candidate = await session.get(Location, item.candidate_location_id)`，再查`candidate.location_group_id`对应的`LocationGroup.name`（为空就是`None`）
3. GPS距离：`candidate`不为空且`candidate.representative_video_sha256`不为空时，`ref_video = await session.get(Video, candidate.representative_video_sha256)`，`video`和`ref_video`的`collect_lon/collect_lat`、高度（`srt_height`不为空用`srt_height`否则`flight_height`，两条视频各自独立判断）都不缺时调`distance_3d_meters`；任一条件不满足直接`gps_distance_meters = None`，不要让这段代码在GPS字段缺失时抛异常

- [ ] **Step 2: 更新所有调用点**

`list_queue`/`get_queue_item`/`generate_queue`/`decide_queue_item`里`_serialize_review_item(item)`的调用都要加`await`和`session`参数——这几处已经在`async def`+有`session`变量的上下文里，改起来是机械替换，不涉及新的判断

- [ ] **Step 3: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/api/routers/location_review.py').read())"`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/app/api/routers/location_review.py
git commit -m "feat: 复核item返回加GPS距离+候选地点身份+视频识别信息"
```

---

## Task 3: 后端——地点/分组列表接口 + 文件夹审查接口 + CORS

**Files:**
- Modify: `aerial_backend/app/api/routers/location_review.py`
- Modify: `aerial_backend/app/main.py`

**Interfaces:**
- Produces：
  - `GET /location-review/locations` → `list[{"id", "city", "site", "representative_frame_path", "location_group_id"}]`（全部`locations`表记录，不分页——当前locations总数42条，量级可以忽略分页）
  - `GET /location-review/groups` → `list[{"id", "name", "confirmed_at", "members": [{"id","city","site"}]}]`（每个group查一下`Location.location_group_id==group.id`的成员列表内联进去）
  - `GET /location-review/folder-audit?storage_path_prefix=...` → `list[{"video_sha256", "storage_path", "location_id", "is_stable", "collect_lon", "collect_lat"}]`（`select(Video).where(Video.storage_path.like(prefix + "%"))`，`prefix`做字面量拼接时用SQLAlchemy的`.like()`参数绑定，不要用f-string拼SQL字符串——`like(f"{prefix}%")`是参数化的，没有注入风险，但要在代码里保持这个写法不要改成原始字符串拼接）

- [ ] **Step 1: 实现3个新路由**，都挂`Depends(require_location_reviewer)`

- [ ] **Step 2: 挂CORS中间件**。`app/main.py`里`app = FastAPI(...)`那行之后加：
  ```python
  from fastapi.middleware.cors import CORSMiddleware
  app.add_middleware(
      CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
  )
  ```
  （这次是内部白名单工具，`allow_origins=["*"]`配合Bearer token鉴权可以接受，不需要精确到具体前端部署域名——跟这次项目"权限不做完整系统"的一贯尺度一致）

- [ ] **Step 3: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/api/routers/location_review.py').read()); ast.parse(open('app/main.py').read())"`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add aerial_backend/app/api/routers/location_review.py aerial_backend/app/main.py
git commit -m "feat: 地点/分组列表接口+文件夹审查接口+CORS"
```

---

## Task 4: 前端脚手架——登录+鉴权+整体布局

**Files:**
- Create: `location_review_web/package.json`、`vite.config.ts`、`tsconfig.json`、`index.html`
- Create: `location_review_web/src/api/client.ts`
- Create: `location_review_web/src/api/types.ts`
- Create: `location_review_web/src/auth/AuthContext.tsx`
- Create: `location_review_web/src/pages/LoginPage.tsx`
- Create: `location_review_web/src/components/AppShell.tsx`
- Create: `location_review_web/src/App.tsx`、`src/main.tsx`
- Test: `location_review_web/src/auth/AuthContext.test.tsx`
- Test: `location_review_web/src/api/client.test.ts`

**Interfaces:**
- Produces：
  - `apiFetch<T>(path: string, options?: RequestInit) -> Promise<T>`（`client.ts`，自动加`Authorization: Bearer <token>`头，收到401时清空token+跳转`/login`，其它非2xx抛出带状态码的Error）
  - `AuthProvider`/`useAuth() -> {token: string | null, login: (token: string) => void, logout: () => void}`（`AuthContext.tsx`，token读写`localStorage`）
  - `AppShell`：侧边栏5个nav项（队列/文件夹审查/地点/分组/一致率看板）+ `<Outlet />`路由出口
  - `types.ts`里的`ReviewItem`类型要跟Task2新增的字段对齐：`screenshot_paths: string[]`、`candidate: {id:number; city:string; site:string; representative_frame_path:string|null; group_name:string|null} | null`、`gps_distance_meters: number | null`、`video_filename: string`、`video_city: string | null`、`video_collected_at: string | null`，以及原有的`visual_match_result`/`llm_verdict`/`human_decision`/`status`等字段

- [ ] **Step 1: `vite create`脚手架**（`npm create vite@latest location_review_web -- --template react-ts`，之后装`react-router-dom`、`vitest`、`@testing-library/react`、`msw`）

- [ ] **Step 2: 写`AuthContext.test.tsx`的失败测试**

```tsx
test("login存token到localStorage，logout清空", () => {
  const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider });
  act(() => result.current.login("abc123"));
  expect(localStorage.getItem("location_review_token")).toBe("abc123");
  act(() => result.current.logout());
  expect(localStorage.getItem("location_review_token")).toBeNull();
});
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd location_review_web && npx vitest run src/auth/AuthContext.test.tsx`
Expected: FAIL（`useAuth`/`AuthProvider`未定义）

- [ ] **Step 4: 实现`AuthContext.tsx`**

- [ ] **Step 5: 跑测试确认通过**

Run同Step3命令，Expected: PASS

- [ ] **Step 6: 写`client.test.ts`的失败测试**（用MSW mock一个返回401的接口，断言`apiFetch`调用后token被清空）

- [ ] **Step 7-8: 跑失败→实现`client.ts`→跑通过**，同Step3-5节奏

- [ ] **Step 9: 实现`LoginPage.tsx`/`AppShell.tsx`/`App.tsx`/路由**（贴token输入框，提交调`login`，`AppShell`包一层"未登录则跳`/login`"的guard，不需要单独测试——纯UI拼装，逻辑已经在Step2-8测过）

- [ ] **Step 10: Commit**

```bash
git add location_review_web/
git commit -m "feat: 前端脚手架+token鉴权+整体布局"
```

---

## Task 5: 队列页面

**Files:**
- Create: `location_review_web/src/pages/QueuePage.tsx`
- Test: `location_review_web/src/pages/QueuePage.test.tsx`

**Interfaces:**
- Consumes：Task4的`apiFetch`/`ReviewItem`类型
- Produces：`QueuePage`——拉`GET /location-review/queue?status=pending`（默认）+status下拉筛选，客户端分页（每页20条，纯前端数组切片，不涉及后端分页参数），列表行展示`video_filename`/`video_city`/`video_collected_at`/`status`，点击行跳转`ReviewDetailPage`（`/queue/:itemId`）

- [ ] **Step 1: 写失败测试**（MSW mock `GET /location-review/queue`返回2条，其中一条对应的video已`location_confirmed=true`——测试断言这条在列表里带有"已通过其它条目处理"的灰显标记文字）

```tsx
test("已锁定视频的陪审item显示灰显标记", async () => {
  server.use(http.get("*/location-review/queue", () => HttpResponse.json([
    { id: 1, status: "pending", video_sha256: "a", video_filename: "x.mp4", video_location_confirmed: true, ... },
    { id: 2, status: "pending", video_sha256: "b", video_filename: "y.mp4", video_location_confirmed: false, ... },
  ])));
  render(<QueuePage />);
  expect(await screen.findByText(/已通过其它条目处理/)).toBeInTheDocument();
});
```

（`video_location_confirmed`字段已经在Task2的`_serialize_review_item`里返回，前端直接用，不需要额外请求）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd location_review_web && npx vitest run src/pages/QueuePage.test.tsx`

- [ ] **Step 3: 实现`QueuePage.tsx`**

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: Commit**

```bash
git add location_review_web/src/pages/QueuePage.tsx location_review_web/src/pages/QueuePage.test.tsx
git commit -m "feat: 复核队列列表页"
```

---

## Task 6: 复核详情页（核心流程）

**Files:**
- Create: `location_review_web/src/pages/ReviewDetailPage.tsx`
- Create: `location_review_web/src/components/ImageGallery.tsx`
- Create: `location_review_web/src/components/MetricsPanel.tsx`
- Test: `location_review_web/src/components/MetricsPanel.test.tsx`
- Test: `location_review_web/src/pages/ReviewDetailPage.test.tsx`

**Interfaces:**
- Consumes：Task4的`apiFetch`/`ReviewItem`；后端`POST /queue/{item_id}/decide`、`POST /videos/{sha256}/unlock`
- Produces：
  - `ImageGallery({images: string[]})`——横向画廊，当前只渲染1张时就是一张大图，不需要特殊处理"只有一张"这个case
  - `MetricsPanel({gpsDistance, visualMatch, llmVerdict})`——方案A布局（底部一行指标文字），三个参数任一为`null`都要显示"无数据"而不是崩溃或留白（对应Review Focus的候选为空/截图失败/LLM失败三种情况）
  - `ReviewDetailPage`——两张图（当前视频的`screenshot_paths[0]`+候选的`candidate.representative_frame_path`，候选为`null`时候选位置显示"新地点候选，无参考图"）+`MetricsPanel`+三个决策按钮（确认同一地点/不是这个候选/新地点，后两个各自对应`same_location`/`different_location`/`new_location`）+解锁按钮（仅当对应视频`location_confirmed`为真时显示）

- [ ] **Step 1: 写`MetricsPanel.test.tsx`失败测试**（分别测：三个参数都给值时正常显示文字；`gpsDistance=null`时显示"GPS距离: 无数据"而不是"GPS距离: null"）

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现`MetricsPanel.tsx`**

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 写`ReviewDetailPage.test.tsx`失败测试**（mock一条`candidate_location_id=null`的item，断言候选区域显示"新地点候选，无参考图"且没有报错/没有尝试渲染`<img>`指向`null`路径）

- [ ] **Step 6: 跑测试确认失败**

- [ ] **Step 7: 实现`ImageGallery.tsx`+`ReviewDetailPage.tsx`**，决策按钮点击调`apiFetch("/location-review/queue/{id}/decide", {method:"POST", body: JSON.stringify({decision, target_location_id})})`，`same_location`/`new_location`需要先让用户选`target_location_id`（简单下拉框，数据来自`GET /location-review/locations`，`new_location`走向是先跳Task8的"新建地点"表单拿到id再回填）

- [ ] **Step 8: 跑测试确认通过**

- [ ] **Step 9: Commit**

```bash
git add location_review_web/src/pages/ReviewDetailPage.tsx location_review_web/src/components/ImageGallery.tsx location_review_web/src/components/MetricsPanel.tsx location_review_web/src/*.test.tsx
git commit -m "feat: 复核详情页(核心对比+决策+解锁)"
```

---

## Task 7: 文件夹审查页面

**Files:**
- Create: `location_review_web/src/pages/FolderAuditPage.tsx`
- Test: `location_review_web/src/pages/FolderAuditPage.test.tsx`

**Interfaces:**
- Consumes：后端`GET /location-review/folder-audit?storage_path_prefix=...`、`POST /location-review/queue/generate`
- Produces：`FolderAuditPage`——文本输入框填路径前缀，提交后列出该前缀下全部视频（`location_id`是否为空用颜色区分），每行一个"生成候选"按钮调`POST /queue/generate`（需要这条视频的`storage_path`，响应里本来就有）

- [ ] **Step 1: 写失败测试**（mock返回0条数据的情况，断言显示"这个前缀下没有视频"——对应Review Focus最后一条）

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: Commit**

```bash
git add location_review_web/src/pages/FolderAuditPage.tsx location_review_web/src/pages/FolderAuditPage.test.tsx
git commit -m "feat: 文件夹审查页"
```

---

## Task 8: 地点管理页面

**Files:**
- Create: `location_review_web/src/pages/LocationsPage.tsx`
- Test: `location_review_web/src/pages/LocationsPage.test.tsx`

**Interfaces:**
- Consumes：后端`GET /location-review/locations`、`POST /location-review/locations`

- [ ] **Step 1-4**：同Task7节奏（列表+新建表单，写一个"新建成功后列表刷新"的测试）

- [ ] **Step 5: Commit**

```bash
git add location_review_web/src/pages/LocationsPage.tsx location_review_web/src/pages/LocationsPage.test.tsx
git commit -m "feat: 地点管理页"
```

---

## Task 9: 分组管理页面

**Files:**
- Create: `location_review_web/src/pages/GroupsPage.tsx`
- Test: `location_review_web/src/pages/GroupsPage.test.tsx`

**Interfaces:**
- Consumes：后端`GET /location-review/groups`、`POST /location-review/groups/merge`

- [ ] **Step 1-4**：地点多选列表+合并按钮，写一个"合并已属于别的分组的location时，后端返回409，前端要显示这个错误信息"的测试

- [ ] **Step 5: Commit**

```bash
git add location_review_web/src/pages/GroupsPage.tsx location_review_web/src/pages/GroupsPage.test.tsx
git commit -m "feat: 分组管理页"
```

---

## Task 10: 一致率看板

**Files:**
- Create: `location_review_web/src/pages/AgreementRatePage.tsx`
- Test: `location_review_web/src/pages/AgreementRatePage.test.tsx`

**Interfaces:**
- Consumes：后端`GET /location-review/agreement-rate`

- [ ] **Step 1-4**：纯展示`total`/`agreed`/`disagreed`/`ready_for_automation`四个数字，写一个"`ready_for_automation=false`时显示明显的'未达标'提示"的测试

- [ ] **Step 5: Commit**

```bash
git add location_review_web/src/pages/AgreementRatePage.tsx location_review_web/src/pages/AgreementRatePage.test.tsx
git commit -m "feat: 一致率看板页"
```

---

## 自我检查记录

- 规格覆盖：设计文档"目标"里6条（队列/核心对比/文件夹审查/地点/分组/一致率/鉴权）每条都有对应任务；"需要的后端小补丁"7条，Task1覆盖第1条，Task2覆盖第2/5/6条，Task3覆盖第3/4/7条；"不新增的"两条（陪审item不后端关闭、不做真分页）在Global Constraints里重申，各页面任务里体现为前端自己处理。
- Review Focus的5条，分别落在：Task6(候选为空、截图失败、LLM失败)、Task4(401跳转)、Task7(空结果)。
- 自我检查时发现Task5需要`video_location_confirmed`字段才能判断陪审item，已经直接补进Task2的Produces列表，不是留给执行者在Task5里现猜。
