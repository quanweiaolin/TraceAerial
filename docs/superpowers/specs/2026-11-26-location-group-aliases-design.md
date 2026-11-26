# 地点分组别名（location_groups aliases）设计

## 背景

客户对同一个物理地点可能有自己的一套命名/编号（比如客户zyt管某个地点叫"ADS3-1"），不同客户叫法还可能不一样。需要在`location_groups`（人工合并后的"同一地点"分组，即"location name"这一层）上记录任意数量客户各自的命名，前端可编辑，没有特殊客户需求时继续用原本的`name`。

## 已确认的设计决策

1. **`location_groups`表加一列`aliases`**：`jsonb NOT NULL DEFAULT '{}'`，扁平字典，key是客户标识（比如`"zyt_name"`），value是该客户对这个地点的命名/编号（比如`"ADS3-1"`）。没有任何客户别名的分组，`aliases`是空对象，照常用`name`显示——别名是附加信息，不替换`name`的默认展示位置。
2. **客户key的候选列表不单独建表**：前端从已经拉取的全部`location_groups`数据里，扫一遍所有`aliases`的key去重，作为下拉框候选；下拉框额外留"新增"选项，手动输入一个全新key。新客户出现时候选列表自然变长，不需要维护单独的枚举/配置。
3. **后端**：
   - `GET /location-review/groups`的返回里，每个group的字典加上`aliases`字段（原样返回JSON对象）。
   - 新增`PATCH /location-review/groups/{group_id}/aliases`，body`{key: str, value: str | null}`：`value`是字符串就新增/覆盖这个key；`value`是`null`就删掉这个key。走现有`record_change`机制记审计（`entity_type="location_group"`, `field_name=f"alias:{key}"`）。
4. **前端**：Groups页面每个分组展示现有别名列表（key: value），加一个"新增/编辑别名"表单——下拉选key（或选"新增"手打）+文本框填值+保存按钮。

## 留给实现计划的部分

- `aliases`字典内部key的命名规范（是否要求固定后缀`_name`之类）——不强制，客户自己叫什么key就是什么，不做校验。
- 别名列表的具体UI排布细节。
