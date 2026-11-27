# Location Naming Audit 设计文档

## 意图与动机

现有的地址聚类流程（`scanner.py:_assign_city_site`，GPS+高度的在线leader算法）给每个location_id分配名字（`Location.site`）只看"哪一条视频恰好触发了新建地址分支"——这条视频文件夹里飞手打的原始site文本是什么，这个location就永久叫这个名字。后续其它视频即使通过GPS+高度聚类归进了同一个location_id，它们自己文件夹里打的原始site文本从未被存过、也从未被拿来跟location当前的名字核对过。

这造成两类现在完全不可见的数据质量问题：

1. **簇内命名漂移**：同一个location_id（已经确认是同一个物理地点）下，飞手在不同批次打的原始site文本不一致（比如拼写不同/打错字），但系统从建立那一刻就只记得第一条视频打的名字，后面的变体全部丢失。
2. **簇间同名提示**：不同location_id（GPS+高度聚类把它们拆开了）如果飞手当年打的原始site文本是一样的，这是很强的"这其实是同一个地方，聚类拆错了"信号——这正是本次会话里location 22/23/24这组案例：三个不同location_id，视觉比对证实是同一次飞行、同一个地方，只是高度差异超过了聚类阈值。

两类信号本质上是同一份数据（"每个location_id下，各个原始site文本出现了几次"）的两种不同查询方向，不需要分别维护两套逻辑。

## 目标

- 对每个location_id，重新从其成员视频的`storage_path`解析出原始site文本（不信任已经写死的`Location.site`，因为那只是"第一条视频"的一次性记录，可能本身就是少数/错误的拼写），统计出现次数。
- 基于这份统计，产出两类待人工确认的提示：命名漂移、合并候选。
- 提供一个新的、轻量的"命名审核"页面，专门给人确认/驳回这些提示——跟现有的`location_review_items`视频级复核队列分开，因为决策形态不一样（审核页面是"选一个正确的名字"，不是"same/different/new location"三选一）。
- 先对现在生产环境已有的全部location跑一次性backfill，同时接进`scanner.py`的入库流程，让这份统计随新视频持续更新。

## Non-goals / Anti-goals

- **绝不改NAS上落盘的文件/文件夹名字**。这个功能只读写数据库里的`Location.site`列，且只在人工点确认之后才写——用户明确要求尽量不碰NAS上的实际文件名。
- **绝不自动合并location_id、绝不自动改名**。哪怕投票结果是100%一致，也只是建议，必须人工点确认才生效——跟项目里其它功能（Groups合并、别名编辑）一贯的"人工点一下才算数"原则一致。
- 这一版**不接飞书群通知**。用户明确说"稳定后再发"，先在页面里跑通，通知是后续独立的改动。
- **不碰GPS+高度聚类算法本身**（`backfill_address_clustering.py`/`_assign_city_site`）。这个功能只审计名字、标记候选，不会改写`video.location_id`——改location_id依然只能通过已有的人工复核流程（`apply_human_decision`）或Groups合并功能完成。

## 约束

- 原始site文本必须在分析时用现有的`session_naming.find_session_match`正则，从`Video.storage_path`重新解析——不能读已经存好的`Location.site`，道理见"动机"部分。
- 合并候选的比较范围限定在同一个city内（不同city即使site文本一样也不可能是同一个物理地点）。**这个city同样要用`find_session_match`从`storage_path`重新解析出来的值分组，不能读`Location.city`列**——`Location.city`没有任何校验（手动建的location可以输入任意大小写/空格的自由文本），用它分组会导致手动录入的location因为大小写不一致，明明该比对却悄悄被漏掉、且不报错。跟site同理：解析不出来的视频直接跳过，不参与任何city分组。

## Approaches

这次讨论下来其实只剩一条路：直接从`storage_path`按location_id统计原始site文本出现次数，在同city范围内比较，用一个新页面展示。曾经考虑过的另一条路——直接读`Location.site`、用正则去掉`_1/_2`后缀来找同名——这是我这次会话里临时查merge候选时用的办法，但已经被排除：`Location.site`只反映"第一条视频"的记录，可能本身就是漂移中的少数样本，去掉后缀也对不上真正该有的投票结果。

## 数据模型（新增）

```sql
CREATE TABLE IF NOT EXISTS location_naming_alerts (
    id                 bigserial PRIMARY KEY,
    alert_type         text NOT NULL CHECK (alert_type IN ('name_drift', 'merge_candidate')),
    city               text NOT NULL,
    location_ids       bigint[] NOT NULL,
    raw_site_votes     jsonb NOT NULL,   -- {"<原始site文本>": {"<location_id>": 出现次数, ...}, ...}
    votes_fingerprint  text NOT NULL,    -- raw_site_votes规范化后的md5，见下面"去重与重新出现"
    suggested_value    text,            -- name_drift: 建议的canonical名字；merge_candidate: 共享的原始site文本
    status             text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'confirmed', 'dismissed')),
    resolved_by        integer REFERENCES operators(id),
    resolved_at        timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_location_naming_alerts_dedupe
    ON location_naming_alerts (alert_type, location_ids, votes_fingerprint);
```

`location_ids`统一用数组：`name_drift`放1个location_id，`merge_candidate`放2个及以上。

**去重与"重新出现"（builder-check发现的设计漏洞，这里是我做的修正，不是开放问题）**：最初设计只按`(alert_type, location_ids)`在`status='pending'`范围内去重，但`raw_site_votes`是从`storage_path`原始文本重新算出来的，人工confirm/dismiss并不会改变这份底层投票数据——下一次扫描（新视频进来触发`detect_naming_alerts`）会重新算出完全一样的统计，而旧的partial unique index只挡`pending`状态，挡不住已经`confirmed`/`dismissed`的记录被原样再插一条新的pending，等于每次扫描都把人工刚确认/驳回过的提示复活。

修正：把`raw_site_votes`规范化（key排序后序列化）算md5存进`votes_fingerprint`，唯一索引改成`(alert_type, location_ids, votes_fingerprint)`、**不再限定status**。`detect_naming_alerts`每次生成候选提示后，先查有没有相同`(alert_type, location_ids, votes_fingerprint)`的记录（任意status）——有就跳过，不管它是pending还是已经被处理过；投票数据真的变了（比如出现第三种拼写、新的location_id加入同名家族）才会生成一条新的pending记录，因为这时fingerprint变了，不会撞现有唯一索引。这样"确认过的问题不会被同样的数据重新打扰"，但"新出现的变化"依然能被发现。

**`merge_candidate`还有一个额外的抑制条件**：如果本来要生成的`merge_candidate`提示涉及的全部`location_ids`已经通过Groups合并成同一个`location_group_id`，就不生成——因为用户走Groups页面合并之后，这组location的原始投票数据完全不会变（合并只写`location_group_id`，不碰视频/site数据），单靠fingerprint去重挡不住"之前没合并过"和"已经合并过"这两种状态产生一样的fingerprint，所以要单独查一次这几个location_id是否已经同组，同组就跳过生成。这是本设计里"去Groups页面合并"这个指引要能真正生效（而不是合并完下次扫描又跳出一样的提示）的必要条件。

## 后端

新服务 `app/services/location_naming_audit.py`：

- `async def compute_raw_site_votes(session, city: str) -> dict[int, dict[str, int]]`：拉这个city下全部`location_id`不为空的视频，重新解析`storage_path`，按location_id分组统计`{原始site文本: 次数}`；解析不出来的视频（不符合会话命名规范的历史数据）直接跳过，不计入任何投票，不报错中断。
- `async def detect_naming_alerts(session, city: str) -> None`：调上面那个函数拿到全量统计后：
  - 某个location_id的统计字典有超过1个key → 生成（或跳过重复的）`name_drift`提示，`suggested_value`取出现次数最高的那个key。
  - 某个原始site文本作为key出现在超过1个location_id的统计字典里 → 生成（或跳过重复的）`merge_candidate`提示，`location_ids`是涉及的那些location_id，`suggested_value`就是那个共享的文本。
  - 两种提示的`raw_site_votes`字段都存完整统计，不只是触发阈值的那部分，方便人工在页面上看到全貌。

新API（挂在`location_review.py`路由里，复用现有`require_location_reviewer`依赖）：

- `GET /location-review/naming-alerts?status=pending`：列出提示，带上涉及location的`city`/`site`方便展示。
- `POST /location-review/naming-alerts/{id}/confirm`，body `{chosen_value?: str}`：
  - `name_drift`：`chosen_value`必填（非空字符串），写入`Location.site = chosen_value`（受现有`UNIQUE(city, site)`约束，撞车直接返回400错误而不是让IntegrityError一路冒到500），`record_change`记一条，alert状态置`confirmed`。
  - `merge_candidate`：这个接口**不自动触发合并**——实际合并依然是人工去已有的Groups合并页面手动做（避免维护两套合并逻辑）。确认这里只是把alert状态置`confirmed`，代表"人已经看过、决定去Groups页面处理"，不需要`chosen_value`。
- `POST /location-review/naming-alerts/{id}/dismiss`：状态置`dismissed`，`resolved_by`/`resolved_at`记上，代表"误报，不用管"。

一次性backfill脚本 `scripts/backfill_location_naming_audit.py`：对当前所有city分别跑一次`detect_naming_alerts`，照搬`issue_location_reviewer_token.py`的脚本模式（`SessionContext`+`argparse`+显式commit）。这个脚本**会写数据库**（插入alert记录），跟之前那个只读的视觉比对脚本不一样——按项目一贯的约定，脚本写好后交给你自己在生产机上跑，不由我代跑。

`scanner.py`接入：`_assign_city_site`给视频分配完`location_id`之后，调一次`detect_naming_alerts(session, city)`（只扫这一个city，不是全量）。这里接受的权衡是：每扫一条新视频都会重新统计一遍该city下全部location的投票——对于这个项目当前的数据量（每个location几十条视频、每个city若干个location）这个开销可以接受；如果以后city规模明显变大，可以改成只增量更新这条视频所在location_id的统计，但现在不需要这么做。

**并发扫描下的插入竞态**：`scanner.py`本身已经有同类竞态的处理先例（`scanner.py:313-320`用`begin_nested()`包一层、`IntegrityError`单独catch，应对定时扫描/阿里云扫描/手动触发扫描互相撞车写同一张videos表的情况）。`detect_naming_alerts`插入`location_naming_alerts`时沿用同一个模式：插入前的"查有没有相同fingerprint"和插入本身不是原子的，两个并发扫描可能同时判断"没有"然后都尝试插入——用`begin_nested()`包住插入、单独catch`IntegrityError`后忽略（说明另一个并发扫描已经抢先插入了同样的记录，不是真正的错误），不让唯一索引冲突把整个per-video扫描循环打断。

## 前端

新页面 `NamingAuditPage.tsx`（走已有的`useApiQuery`模式）：

- 列出`status=pending`的提示，`name_drift`展示该location当前名字+各个原始site文本及出现次数+一个选择/填写正确名字的输入框+确认按钮；`merge_candidate`展示涉及的几个location_id（city/site）+共享的原始文本+次数，按钮是"去Groups页面合并"（跳转，不在这里做）和"误报，忽略"。
- 加路由+导航入口，跟现有的GroupsPage等页面并列。

## Review Focus

- 一个location_id下全部成员视频原始site文本完全一样（没有漂移）的正常情况，不应该生成任何提示。
- 某些历史视频`storage_path`不符合会话命名规范、解析不出raw site——这些视频应该被跳过不计入投票，不能让整个检测函数因为解析失败而中断。
- 同一个`(alert_type, location_ids)`组合重复跑检测，不能生成重复的pending记录。
- 只有1条成员视频的location_id，统计字典天然只有1个key（次数=1）——不算漂移，不该生成`name_drift`提示。
- 确认`name_drift`时，如果选的名字跟同city下另一个location的`site`撞车，接口要返回清楚的400错误，不能让`UNIQUE(city, site)`的IntegrityError直接冒成500。
- confirm/dismiss一条提示之后，紧接着再跑一次检测（投票数据没有任何变化），不应该重新生成同样的pending提示——这是上面"去重与重新出现"修正要保证的行为，必须有测试盯着。
- 一组location已经通过Groups合并成同一个`location_group_id`之后再跑检测，不应该为这组location重新生成`merge_candidate`提示。
- 两个不同city下出现了同样的原始site文本，不应该被当成合并候选（city分组用重新解析的值，不是信任`Location.city`）。
