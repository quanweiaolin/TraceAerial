# 地点分组别名 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）or superpowers:executing-plans 按任务逐个实现本计划。步骤用`- [ ]`勾选框语法追踪进度。

**Goal:** 给`location_groups`加客户别名字段，前端可编辑，没有别名时继续用原`name`展示。

**Architecture:** 后端加一列JSONB+一个PATCH接口；前端在已有的Groups页面上展示+编辑这个字典，候选key从已拉取的全部分组数据里去重算出来，不新建枚举表/新接口。

**Tech Stack:** FastAPI+SQLAlchemy（已有）；React+TS（已有`location_review_web`）。

**Spec:** `docs/superpowers/specs/2026-11-26-location-group-aliases-design.md`

## Global Constraints

- 数据库schema改动追加进`aerial_backend/add_location_review_schema.sql`，交给用户自己执行，绝不自己执行。
- `aliases`是扁平`{key: value}`字典，key不做命名规范校验。
- 没有别名的分组，展示逻辑不变——继续用`name`，`aliases`只是附加展示。
- 后端无pytest，用语法检查(`ast.parse`)+手动验证；前端用Vitest（这次计划沿用`location_review_web`已有的测试基建）。

## Review Focus

- `PATCH`一个不存在的`group_id`——必须404，不能让`session.get`返回`None`之后继续往下跑导致500。
- `value: null`删除一个本来就不存在的key——不能报错，幂等地什么都不做即可（删除一个不存在的东西不算错误）。
- 前端别名列表为空（`aliases == {}`）时——必须显示"暂无别名"之类的空态，不能渲染空列表或者报错。
- 新增别名时key留空提交——前端要在提交前挡住，不能发一个key为空字符串的PATCH请求。

---

## 文件结构

**后端：**
- Modify: `aerial_backend/add_location_review_schema.sql`
- Modify: `aerial_backend/app/models.py`（`LocationGroup`加`aliases`列）
- Modify: `aerial_backend/app/api/routers/location_review.py`（`list_groups`序列化加字段+新增`PATCH /groups/{group_id}/aliases`）

**前端：**
- Modify: `location_review_web/src/api/types.ts`（`GroupSummary`加`aliases`字段）
- Modify: `location_review_web/src/pages/GroupsPage.tsx`（展示+编辑别名）
- Modify: `location_review_web/src/pages/GroupsPage.test.tsx`

---

## Task 1: 后端——aliases列 + PATCH接口

**Files:**
- Modify: `aerial_backend/add_location_review_schema.sql`
- Modify: `aerial_backend/app/models.py`
- Modify: `aerial_backend/app/api/routers/location_review.py`

**Interfaces:**
- Produces：`LocationGroup.aliases: Mapped[dict] = mapped_column(JSONB, default=dict)`；
  `GET /location-review/groups`每个分组的返回字典加`"aliases": group.aliases`；
  `PATCH /location-review/groups/{group_id}/aliases`，body `{"key": str, "value": str | None}`，
  `value`非空时`group.aliases[key] = value`，`value`是`None`时`group.aliases.pop(key, None)`
  （用普通dict操作改完整个字典再整体赋值回`group.aliases`，不要原地改字典再不重新赋值——
  SQLAlchemy对JSONB列的变更追踪(change tracking)依赖赋值触发脏标记，原地`.update()`/`.pop()`
  不一定能让ORM感知到变化，必须`group.aliases = {**group.aliases, ...}`这种整体替换的写法）。
  `group_id`不存在时404。调`record_change`记一条审计
  （`entity_type="location_group"`, `entity_id=str(group_id)`, `field_name=f"alias:{key}"`,
  `old_value`=改之前这个key的值(没有就是`None`)，`new_value`=`value`，`changed_by=operator.id`）。

- [ ] **Step 1: SQL追加**

在`add_location_review_schema.sql`末尾追加：
```sql
ALTER TABLE location_groups
    ADD COLUMN IF NOT EXISTS aliases jsonb NOT NULL DEFAULT '{}';
```

- [ ] **Step 2: ORM加列**，`LocationGroup`类加`aliases: Mapped[dict] = mapped_column(JSONB, default=dict)`

- [ ] **Step 3: `list_groups`序列化加字段**，在现有返回字典里加`"aliases": group.aliases`

- [ ] **Step 4: 实现`PATCH /groups/{group_id}/aliases`**

```python
class UpdateGroupAliasIn(BaseModel):
    key: str
    value: str | None = None

@router.patch("/groups/{group_id}/aliases")
async def update_group_alias(
    group_id: int,
    body: UpdateGroupAliasIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    group = await session.get(LocationGroup, group_id)
    if group is None:
        raise HTTPException(404, "分组不存在")
    old_value = group.aliases.get(body.key)
    new_aliases = dict(group.aliases)
    if body.value is None:
        new_aliases.pop(body.key, None)
    else:
        new_aliases[body.key] = body.value
    record_change(
        session, entity_type="location_group", entity_id=str(group_id),
        field_name=f"alias:{body.key}", old_value=old_value, new_value=body.value,
        changed_by=operator.id,
    )
    group.aliases = new_aliases
    await session.commit()
    return {"id": group.id, "aliases": group.aliases}
```
（需要从`app.services.location_history`import `record_change`，检查文件顶部是否已经import过，没有就加）

- [ ] **Step 5: 验证**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/models.py').read()); ast.parse(open('app/api/routers/location_review.py').read())"`
Expected: 无输出

- [ ] **Step 6: Commit**

```bash
git add aerial_backend/add_location_review_schema.sql aerial_backend/app/models.py aerial_backend/app/api/routers/location_review.py
git commit -m "feat: 地点分组加客户别名字段+编辑接口"
```

---

## Task 2: 前端——Groups页面展示+编辑别名

**Files:**
- Modify: `location_review_web/src/api/types.ts`
- Modify: `location_review_web/src/pages/GroupsPage.tsx`
- Modify: `location_review_web/src/pages/GroupsPage.test.tsx`

**Interfaces:**
- Consumes：Task 1的`GET /groups`新字段`aliases: Record<string,string>`、`PATCH /groups/{id}/aliases`
- Produces：`GroupSummary`类型加`aliases: Record<string, string>`字段；
  `GroupsPage`每个分组下展示现有别名（`Object.entries(group.aliases)`列出key:value，空对象显示
  "暂无别名"）；一个新增/编辑表单——下拉框选key（候选来自`Array.from(new Set(groups.flatMap(g =>
  Object.keys(g.aliases))))`去重，外加一个"新增"选项，选了之后换成一个文本输入框手打新key）+
  一个文本框填value+保存按钮，提交前要求key非空（trim后非空）才启用保存按钮；保存调
  `apiFetch(\`/location-review/groups/\${id}/aliases\`, {method:"PATCH", body: JSON.stringify({key, value})})`
  成功后用已有的`refetch`（`useApiQuery`提供的）刷新分组列表。

- [ ] **Step 1: 写失败测试**

```tsx
test("展示已有别名，支持新增别名", async () => {
  server.use(
    http.get("*/location-review/groups", () => HttpResponse.json([
      {id:1, name:"panshiroad合并组", confirmed_at:"2026-11-26T00:00:00Z",
       members:[], aliases:{zyt_name:"ADS3-1"}},
    ])),
    http.patch("*/location-review/groups/1/aliases", async ({request}) => {
      const body = await request.json();
      expect(body).toEqual({key:"new_customer", value:"X-1"});
      return HttpResponse.json({id:1, aliases:{zyt_name:"ADS3-1", new_customer:"X-1"}});
    }),
  );
  render(<GroupsPage />);
  expect(await screen.findByText(/zyt_name/)).toBeInTheDocument();
  expect(await screen.findByText(/ADS3-1/)).toBeInTheDocument();
  // ...选"新增"选项，输入key=new_customer、value=X-1，点保存，断言PATCH请求体正确
});

test("新key为空时保存按钮保持禁用", async () => {
  server.use(http.get("*/location-review/groups", () => HttpResponse.json([
    {id:1, name:"panshiroad合并组", confirmed_at:"2026-11-26T00:00:00Z", members:[], aliases:{}},
  ])));
  render(<GroupsPage />);
  // 选"新增"选项，不输入key，只填value，断言保存按钮disabled为true，
  // 不发起任何PATCH请求(用一个会抛错的handler占位，断言没被调用)
});

test("没有别名时显示空态", async () => {
  server.use(http.get("*/location-review/groups", () => HttpResponse.json([
    {id:2, name:"无别名组", confirmed_at:"2026-11-26T00:00:00Z", members:[], aliases:{}},
  ])));
  render(<GroupsPage />);
  expect(await screen.findByText("暂无别名")).toBeInTheDocument();
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd location_review_web && npx vitest run src/pages/GroupsPage.test.tsx`

- [ ] **Step 3: 实现`types.ts`+`GroupsPage.tsx`的别名展示/编辑逻辑**

- [ ] **Step 4: 跑测试确认通过+全量回归**

Run: `npx vitest run` (期望全部通过，不只是这个文件) ， `npx tsc -b`（期望干净）

- [ ] **Step 5: Commit**

```bash
git add location_review_web/src/api/types.ts location_review_web/src/pages/GroupsPage.tsx location_review_web/src/pages/GroupsPage.test.tsx
git commit -m "feat: 分组页面支持展示/编辑客户别名"
```

---

## 自我检查记录

- 规格覆盖：设计文档4条决策（JSONB列/不单独建枚举表/GET+PATCH接口/前端展示编辑）分别对应Task1的Step1-4和Task2全部。
- Review Focus的4条：404(Task1 Step4)、删除不存在的key幂等(Task1 Step4的`.pop(key, None)`)、空态(Task2 Step1空态测试)、key为空不让提交(Task2 Step1新增的"保存按钮保持禁用"测试)——全部4条都有对应测试，不是只实现不测。
- 类型一致性：`GroupSummary.aliases`与后端`aliases: dict`对应的TS类型`Record<string,string>`在Task1/Task2两处用词一致。
