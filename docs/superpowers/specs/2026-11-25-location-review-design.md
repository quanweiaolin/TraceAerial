# 地址（location）人工复核 + 视觉校验设计（后端部分）

## 背景与目的

现状：`videos.location_id`完全靠SRT GPS+高度聚类的"领导者算法"自动算出
来（`app/services/scanner.py`的`_assign_city_site`），从来没有视觉特征
校验，也没有人工复核环节。活跃视频25720条，只有2306条（约9%）真正打上
了`location_id`，覆盖率低；而且一旦一个视频被判定为"不稳定"或GPS缺失，
永远不会再被纳入聚类。

另一个问题是颗粒度太细：`locations`表里的每一行（如`panshiroad_1`、
`panshiroad_2`）是GPS聚类产生的"物理簇"，可能同一个真实地点因为飞手
分批飞、GPS漂移等原因被拆成了多个簇，现有设计完全没有"这几个簇其实是
同一个地点"这一层概念。

本设计解决：给`location_id`的判定加一层**人工复核**（配合视觉特征比对
和LLM影子判断），并新增一层**地点分组（location_group）**，让"是不是
同一地点"最终可以由人确认、也可以在未来由LLM自动接管。

本设计只覆盖**后端**（数据模型+API+视觉比对/LLM接入）。前端（重型、
独立页面）是后续单独的计划，不在本计划范围。

## 目标 / 非目标 / 反目标

**目标**
- 给`videos.location_id`加人工确认+锁定机制：一旦人工确认，任何自动化
  重跑（现有GPS聚类、未来的视觉/LLM判断）都不能覆盖，除非人自己改。
- 新增`location_group`概念：允许人工把若干个`locations`行（物理簇）
  合并标记为同一个逻辑地点，同样可锁定。
- 借鉴（不是import）`extern/AD_pipeline_1/apps/preparation/data_prepare`
  里SIFT特征匹配+RANSAC单应矩阵估计的思路，在`aerial_backend`自己写一份
  独立实现，产出"两张截图是否同一场景"的自动信号，作为人工复核的辅助
  依据（不是唯一依据）。
- LLM（Claude，走现有`app/llm_agent/client.py`）对同一批候选先"影子
  判断"（不影响实际结果，只记录），跟人工决策持续对比；达到"连续50次
  测试、跟人工差异为0"才允许后续切换成LLM自主决策（本计划只做记录和
  对比计算，不做自动切换开关——那是后续计划的事）。
- 权限：不做完整登录系统，只给白名单内的几个人发token，对`operators`
  表加一个权限位。
- 完整的变更历史：谁在什么时候把什么从什么改成了什么。
- 给未来"下游收到地址变更通知"留一个事件记录点，但不实际对接任何下游
  （目前确认下游未接入`locations`表）。

**非目标**
- 不做前端（另开计划）。
- 不做"LLM自动替人决策"的开关/自动化——只做记录和一致率计算。
- 不改动现有`_assign_city_site`的聚类算法本身（SRT GPS聚类逻辑不变），
  只加"如果视频已被人工锁定，跳过聚类"这一条新判断。
- 不对视觉匹配阈值（内点数≥12、内点率≥0.35）做改动或重新标定——用户
  已确认沿用现有值。
- 不做完整的用户登录系统/会话管理——只做白名单token鉴权。

**反目标（即使跑起来了也算失败）**
- 任何自动化重跑（聚类/批量重算）悄悄覆盖了人工已锁定的`location_id`
  或已确认的`location_group`合并。
- 把LLM的判断直接当成最终结果写入`location_id`（本阶段LLM只能是旁路
  影子判断，不能直接产生业务后果）。
- 视觉匹配/截图相关代码跟`extern/AD_pipeline_1`产生任何耦合——不import
  那边的任何模块，只借鉴算法思路（SIFT+RANSAC+inlier阈值），在
  `aerial_backend`里完全自包含实现。

## 约束

- 数据库schema改动以`.sql`文件形式交给用户自己执行（项目一贯规矩，
  绝不自己执行数据库写操作），跟仓库里现有的`add_*.sql`文件同一惯例。
- `aerial_backend`目前没有自动化测试框架（没有pytest依赖），按现有
  验证方式（语法/import检查+手动curl跑起来的服务）来，不单方面引入
  pytest。
- 不import`extern/AD_pipeline_1`的任何模块、不碰`sys.path`——视觉比对
  在`aerial_backend`里完全自包含实现，只借鉴算法思路（2026-11-25用户
  明确要求：自己写，不嵌套引用别的仓库）。
- `operators`表已有`role`字段（用于pilot/annotator等已有角色），新的
  复核权限不要复用/覆盖这个字段，用独立的布尔列，避免跟现有语义混淆。

## 已确认的设计决策（对话中逐条确认过）

1. **videos表锁定字段**：挂在`videos`表（不是`locations`表）——锁的是
   "这条视频的地址判定"，跟视频一对一。新增`location_confirmed`
   （boolean）+`location_confirmed_by`（FK `operators.id`，可空）+
   `location_confirmed_at`（timestamptz，可空）。`_assign_city_site`
   跑聚类前，先跳过`location_confirmed=true`的视频（不参与、也不覆盖）。

2. **location_group（location_name的存储方式）**：新建`location_groups`
   表（`id`、`name`、`confirmed_by`FK operators、`confirmed_at`、
   `created_at`），`locations`表加可空的`location_group_id`外键。
   合并决定（"这几个locations是同一个地点"）的锁定独立于videos表那把
   锁——这是两层不同的决策（视频归哪个location / 哪些location是同一
   地点），分别锁，互不干扰。

3. **视觉特征比对自己写，只借鉴AD_pipeline_1的思路，不import它的代码**
   （2026-11-25改——最初考虑过直接复用`extern/AD_pipeline_1`的函数，
   用户明确要求"自己写，我只借鉴思路，别嵌套引用"）：
   - 截图提取：`aerial_backend`自己写，用`ffmpeg`子进程读取视频第一帧
     （跟`app/services/video_metadata.py`用`subprocess`调`ffprobe`
     是同一惯例，不新增ffmpeg之外的二进制依赖）。
   - 自动比对：自己写SIFT提取+`cv2.BFMatcher`knn匹配+
     `cv2.findHomography(..., cv2.RANSAC, ...)`估计单应矩阵——算法结构
     借鉴`apps/preparation/data_prepare/point_matching.py`的
     `_image_homography`，但代码是`aerial_backend`自己的实现，不import
     那个模块。新增`opencv-python-headless`依赖（SIFT是OpenCV的算法，
     绕不开这个库）。
   - 自动接受阈值：数值沿用`match_policy.with_auto_acceptance`的"内点数
     ≥12、内点率≥0.35"（用户已确认这两个数字本身不改），但判断逻辑是
     `aerial_backend`自己写的一个小函数，不import`match_policy.py`。

4. **人工复核队列**：新增`location_review_items`表，记录：待复核的
   视频、对比的候选location（或"新建地点"）、截图路径、视觉比对结果
   （`auto_accepted`/`auto_reason`/`inliers`/`inlier_ratio`，存JSONB）、
   LLM判断结果（JSONB，含判断+置信度+理由）、人工决策、决策人、决策
   时间、状态。**不在这张表里存截图的HTML/展示内容**——只存文件路径，
   展示交给后续的前端计划。

5. **变更历史**：新增`location_change_history`表，通用设计（不分别为
   videos和locations各开一张表）：`entity_type`（'video'|'location'|
   'location_group'）、`entity_id`、`field_name`、`old_value`、
   `new_value`、`changed_by`FK operators、`changed_at`、`reason`
   （可空文本）。

6. **权限**：`operators`表加`can_review_location` boolean（默认
   false，只给白名单的几个人手动置true）。新增`operator_api_tokens`
   表（`id`、`operator_id`FK、`token_hash`、`created_at`、
   `revoked_at`可空）——只存哈希，不存明文token；发token用一个CLI脚本
   一次性打印，之后再也查不到明文。FastAPI依赖函数校验
   `Authorization: Bearer <token>`，查表+确认`can_review_location=true`
   +未撤销。

7. **LLM影子判断的"达标线"**：连续50次测试、跟人工决策的差异为0才
   允许后续讨论"切换成LLM自主决策"（本计划不做切换开关，只做记录+
   一致率计算函数，供以后判断是否达标用）。如果事后发现某次人工判断
   本身是错的、被人工纠正了，这一条历史判断不计入"LLM判断错误"的统计
   （按纠正后的人工结果重新核对那条LLM判断是否一致）。

8. **下游通知留口子**：`location_change_history`本身的写入就是"变更
   事件"的记录，暂不额外建事件总线/webhook——确认过下游
   （`AD_pipeline_1`的`data_prepare`）目前只读`videos`表的
   `storage_path`/`duration_seconds`/`collected_at`/`deleted_at`
   四个字段（通过Metabase API），完全不碰`locations`表，所以本次
   改动对下游零兼容性风险，不需要主动通知。

## 已识别的风险（供执行时留意，非本计划要解决的问题）

- 视觉匹配阈值是为"几何对齐"任务调的，用在"同地点判断"语义不同，
  可能产生较多假阴性/假阳性——本计划按用户决定不调整阈值，但人工复核
  这一步正是为了兜底这个风险，不能把`auto_accepted`当作自动决策。
- 自己实现的单应矩阵有效性校验（对应`AD_pipeline_1`那边的
  `validate_homography`）需要重新写一个简化版（比如检查矩阵可逆、
  缩放/剪切分量在合理范围），不要求跟`AD_pipeline_1`完全一致，这是
  本计划自己要定的校验逻辑，不是照抄。
