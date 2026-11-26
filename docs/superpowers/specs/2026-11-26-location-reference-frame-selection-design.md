# 地址参考图（底图）自动选择 设计（补充）

## 背景

现状（`docs/superpowers/specs/2026-11-25-location-review-design.md`已实现的部分）：
`locations.representative_frame_path`只在`apply_human_decision`里被动回填一次——"谁第一个被人工确认进这个location，谁的截图就永久当参考图"，没有重选、没有质量判断。

2026-11-26实际排查生产数据发现这个问题是真实的：location 22/23/24三个GPS聚类，22有50条视频、23和24各只有1条，三者经纬度几乎完全一样，但23/24因为SRT遥测高度读数异常（分别偏离基准约36米和13米）被三维距离算法拆成了独立location——这类"同一地点但聚类拆开了"的情况，光靠"第一个确认者的截图"这种被动策略既不能保证参考图质量，也没有纠错入口（现在也没有"更换参考图"的功能）。

本设计解决：参考图怎么自动选、怎么允许人工纠正、换了参考图之后已有的视觉比对数据怎么处理。

参考了`extern/AD_pipeline_1`的"底图"（basemap/anchor）机制——确认过那边是**纯人工点选**，没有任何自动评分算法（`data_prep.py:70`的`replace_basemap`直接用请求体传来的`video_path`，`static/workbench/app.js:447-472`就是个图片画廊+点选）。preparation能这样做是因为那边本来就是人工逐场景走流程；咱们这边是批量/自动化场景，不能照搬纯人工方案，需要一个自动默认值+人工纠错口子的折中设计。

## 已确认的设计决策

1. **初始自动选择算法（仅此一次）：GPS centroid最近邻**。一个location第一次需要参考图时（目前的触发点就是`apply_human_decision`里"这个location还没有参考图"那个回填分支），对这个location下`deleted_at IS NULL`的全部视频，算它们`(collect_lon, collect_lat, height)`的几何中心（centroid；height取`srt_height`优先、否则`flight_height`，跟现有`_resolve_height`规则一致），选**离中心点三维距离最近**的那条视频，用它的截图当参考图。纯几何计算，不需要额外跑视觉特征，成本接近零。
   - 不采用preparation式的纯人工点选（咱们场景无法要求每个location都有人工介入做"第一次"这个动作）。
   - 不采用视觉覆盖度评分（`diverse_candidates`/`estimate_candidate`那套两两特征比对）——计算量不小，而且本系统本来就有人工复核兜底，没必要为了"自动选到最好的那张"再加一层复杂度。

2. **初始确认之后，参考图只能由人工手动更换**（2026-11-26明确：不要"簇成员变化自动重选"这一条——理由是下游数据集开发是照着这个参考图/底图做的，底图必须稳定，不能被自动化悄悄换掉，任何变更都要是人的主动决定）。这是preparation那个"更换底图"思路的对应物：在location的某个成员视频列表里，人工选一条视频把它设为参考图，覆盖当前那张。
   - 不需要额外的"锁定"字段区分"自动选的"还是"人工设的"——因为没有竞争的自动重选逻辑去跟人工决定抢，初始自动选择本身也只发生一次。谁在什么时候换过参考图，靠现有的`location_change_history`审计记录就够（`entity_type="location"`, `field_name="representative_frame_path"`），不需要专门新增一列。

3. **参考图变了，所有待处理的视觉比对数据必须重算**——这是本次新加的、之前设计没覆盖的要求（触发来源现在只有一个：人工手动更换，不再有自动重选这个触发源）：
   - 范围：**只影响`status="pending"`且`candidate_location_id`指向这个location的`location_review_items`**——这些item的`visual_match_result`/`llm_verdict`是拿旧参考图算出来的，参考图一换就是过时数据，必须重算，不能留着误导人工复核。
   - **不影响`status="decided"`的历史记录**——人工已经做出的决策、以及当时看到的视觉/LLM数据是历史快照，不因为后来参考图变了就回头篡改，保留审计真实性；这些历史决策不受影响、不需要重新评估（如果需要重新评估，走现有的"解锁"流程人工重新走一遍）。
   - 重算方式：不需要重新提取视频截图（`pending` item自己的`screenshot_path`还在，没变），只需要用这条视频已有的截图路径 + location**新的**参考图路径，重新调一次`compare_frames`+`judge_async`，更新这条item的`visual_match_result`/`llm_verdict`/`llm_verdict_at`。
   - 触发时机：唯一的触发源是第2点"人工手动更换参考图"这个动作；第1点的"初始自动选择"本身不触发重算（此时这个location还没有参考图，自然也没有已存在的pending item需要重算）。

## 留给实现计划的部分

- 具体的"手动更换参考图"UI交互（在哪个页面、怎么选视频）。
- 重算是同步做（在更换/重选的同一个请求里跑完）还是丢进后台任务——取决于同时受影响的pending item数量级，实现时再按实际情况决定，不在本设计文档里预先定死。
- centroid计算的具体SQL/代码实现细节。
