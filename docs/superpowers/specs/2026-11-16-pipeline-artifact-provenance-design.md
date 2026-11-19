# 下游Pipeline产物登记设计（代码版本 + 数据来源）

## 背景与目的

TraceAerial后端只管到原始视频入库（`videos`表，sha256去重）。下游还有
一串各自独立的stage——感知跟踪处理（`extern/AD_pipeline_1`，本次先落地
的这个）、以后还会有别的同学做的SOTIF场景提取、数据集生成等——每个stage
各自一个仓库、各自按需更新，不是统一代码库、不走CI/CD。

现状问题：这些stage产出了什么、用的哪个代码版本、源自哪个原始视频，
目前完全没有集中记录，只能靠口头/文件夹约定。本设计解决"如何以尽量低的
改动成本，让这些产物在后端留一笔可查询的账"。

`aerial.annotation_batches`（另一条独立的Claude/deepseek多模态标注流水线）
也有类似但更局部的问题，作为本设计的第二个落地点一并处理，但方案不同
（见"附：annotation_batches”）。

## 目标 / 非目标 / 反目标

**目标**
- 每个stage产出的"交接产物"（会被别的stage或人直接消费的那份东西），
  在后端留一条可查询记录：源自哪个原始视频、代码版本、关键参数/指标、
  产物位置。
- 对现有学生代码改动尽量小——不改调度/并发逻辑，不要求学生改造自己的
  仓库结构。
- 后续新增别的同学的stage时，接入方式一致、不用后端加新表/新endpoint。

**非目标**
- 不做"产物本身"的集中存储/备份——只登记"在哪、怎么来的"，原始文件
  还放在各自的NAS路径上。
- 不追求对LLM等非确定性步骤的逐位可复现——记录足够"定位到是哪个版本
  产生的"即可，不保证重跑结果完全一致。
- 不覆盖`visualization`这类纯QA、没有下游消费者的产物（可以后续再补，
  不在本次范围）。

**反目标（即使技术上跑起来了，以下情况也算失败）**
- 学生需要大改自己仓库的结构/调度方式才能接入。
- 强依赖"学生记得手动调用"——能自动探测的（代码版本）必须自动探测，
  不能要求人手填。

## 约束

- 下游各stage不在同一个仓库，不能假设统一的git仓库布局，也不能假设
  调用方有任何特定的内部架构（多进程/单进程/配置怎么传）——注册这件事
  本身不该要求调用方为了配合它改造自己的代码结构。
- 原始视频的身份用路径（`available_videos.csv`的`absolute_path`，
  直接取自`aerial.videos.storage_path`），不是sha256——sha256要由
  后端按路径反查，不要求学生代码自己对大视频文件算哈希。

## 已确认的设计决策（对话中逐条确认过）

1. **stage颗粒度**：按"是否会被别的同学的stage直接消费"划分，不按代码内部
   分了几步划分。`AD_pipeline_1`内部的detection/stabilization/alignment/
   tracking/postprocess/standardized_output全部合并登记为**一个**stage
   （确认过`standardized_output`这一步在实际配置里永远会跑，不存在
   "有时候跑不到这一步"的分支，所以登记点是固定的，不需要按run_func
   动态选择落点）。
2. **代码版本来源**：`register_artifact`调用现场自己探测git commit+
   dirty标记（见下面"方案比较"），不依赖调用方的内部架构往下传——
   这是一个给架构各不相同的下游stage共用的服务，接入成本不该取决于
   对方内部怎么传参。
3. **输入血缘**：登记时传原始视频的**绝对路径**（可以是一个或多个，
   为以后"多视频合并产出一个东西"的stage留口子），后端按
   `videos.storage_path`反查`sha256`存成固定字段；不强求链路上每一跳
   都精确指向"上一个stage具体哪一条记录"，那个粒度损失本次接受。
4. **产物描述自解释**：除了固定字段（输入视频sha256数组、代码仓库+
   commit+dirty、stage代码、产物主路径），其余所有stage特定的东西
   （用了哪个检测模型、tracking参数指纹、行数等）都塞进一个不定schema
   的JSONB，不单独开列。

## 方案比较：代码版本怎么传到登记调用

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A.（采用）调用现场自己探测** | `register_artifact`被调用的那一刻，从"调用代码所在的git仓库"现场跑`git rev-parse HEAD`+`git status --porcelain` | 每次调用多一次毫秒级subprocess，但零假设——任何stage接入，不管是多进程/单进程/什么语言外壳调这个HTTP接口，都不用适配 |
| B. 父进程算一次，往下传 | 复用`archive_run_config`算好的值，塞进调用方自己的某个全局状态往下传 | 省掉重复探测，但绑死在"调用方得有个全局状态可以往下传"这个假设上，换一个架构不同的stage就要重新设计传递方式 |

原来选的是B，重新考虑后改选A：这是一个给"各自独立、架构各不相同"的下游
stage用的服务，接入成本不该取决于对方的内部实现形状（多进程/单进程/
配置怎么传）。A的代价（每次调用多一次毫秒级git命令）跟视频处理动辄
几分钟一次相比完全可以忽略，用这点开销换"接入任何新stage都不用专门
适配"，划得来。

## 落地设计

### 后端新增

**新表** `aerial.pipeline_artifacts`（新建，不复用`video_pipeline_status`——
那张表是"一视频一行"的当前状态表，这里要的是"一个产物可以有多个输入视频、
允许同一视频+stage出现多条历史记录"）：

```sql
CREATE TABLE aerial.pipeline_artifacts (
    id              bigserial PRIMARY KEY,
    stage_code      text NOT NULL REFERENCES aerial.pipeline_stages(code),
    input_video_sha256 bytea[] NOT NULL,   -- 一个或多个，元素引用 videos.sha256
    code_repo       text,                  -- 比如 "AD_pipeline_1" 或仓库URL
    code_commit     text,                  -- git rev-parse HEAD
    code_dirty      boolean,
    output_path     text NOT NULL,         -- 产物主文件/目录的绝对路径
    upstream_artifact_id bigint REFERENCES aerial.pipeline_artifacts(id),  -- 可选，紧邻上游明确时填
    metadata        jsonb NOT NULL DEFAULT '{}',  -- stage自定义：模型/参数指纹/行数等
    operator_name   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON aerial.pipeline_artifacts USING gin (input_video_sha256);
```

`pipeline_stages`需要先插入这位同学的一行（`code`给一个短名字，比如
`ad_perception_tracking`，`name_cn`="感知跟踪处理"）。

**新API**：`POST /pipeline/artifacts`，请求体：

```json
{
  "stage_code": "ad_perception_tracking",
  "input_video_paths": ["/mnt/Expansion2/.../xxx.MP4"],
  "code_repo": "AD_pipeline_1",
  "code_commit": "a1b2c3d...",
  "code_dirty": false,
  "output_path": "/.../xxx_STD_TRK.csv",
  "upstream_artifact_id": null,
  "metadata": {"detection_model": "...", "config_fingerprint": "..."},
  "operator_name": "quanwei"
}
```
后端按`input_video_paths`逐个查`videos.storage_path`拿到`sha256`（查不到
就整体422报错，提示路径不在`videos`表里——这是故意收紧的校验，避免登记
内容跟实际入库数据脱节）。

### 学生代码（`AD_pipeline_1`）改动

1. 新增一个**完全自包含、零相对import**的客户端模块（建议放在
   `nds_code/utils/artifact_registration.py`，只用标准库——这样它就是
   一个能直接丢进任意学生仓库的单文件，不依赖`nds_code`包的其他模块，
   也不依赖`config`字典里有任何特定字段。以后别的同学接入新stage，
   "安装"这件事就是：给他们这一个文件 + 一行"放哪、怎么调"的说明，
   不需要pip装任何依赖）：
   `register_artifact(stage_code, input_video_paths, output_path, metadata)`。
   内部自己定位调用方所在的git仓库（从这个函数文件自身`__file__`往上找
   `.git`，不是从`os.getcwd()`，避免调用方在别的目录下跑脚本时找错
   仓库），现场跑`git rev-parse HEAD`/`git status --porcelain`拿到
   commit+dirty，再POST给后端新接口。
   - 后端返回422（输入路径查不到对应视频）——这是数据本身有问题，按
     `logger.error`高优先级记录，不写入待重试队列（重试也不会成功）。
   - 网络失败/后端不可达（连不上、超时）——**不是**数据问题，是
     "这次恰好联系不上"，不能直接丢：把这次完整的请求内容(JSON)追加
     写进本地一个待重试文件(比如`~/.aerial_pipeline/pending_artifacts.jsonl`，
     一行一条)，打一条warning日志，不抛异常中断pipeline。
   - `register_artifact`每次被调用时，**先**尝试把待重试文件里攒着的
     旧记录逐条补发一遍（成功的从文件删掉，还失败的留着），再处理
     这次新的——不需要学生记得手动做什么，下次随便哪次pipeline跑
     起来、后端刚好恢复了，旧账自动补上。
   - 另外留一个最简单的手动补发入口（命令行跑
     `python -m nds_code.utils.artifact_registration --retry-pending`），
     不用等下次pipeline跑就能手动确认补发。
   - **补录古早数据用的可选参数`code_commit_override`**：正常跑pipeline
     时不传这个参数，照旧自动探测当前HEAD（见上面"代码版本来源"）。
     补录脚本（针对之前已经产出、这次只是事后补登记的数据）可以：
     显式传`code_commit_override=None`表示"确实想不起来当时是哪个
     commit了，不要猜"——这种情况`code_commit`/`code_dirty`都留空，
     不会偷偷填成"当前HEAD"（那对古早数据是误导，不是不精确，是错）；
     或者传一个学生记得的commit值（哪怕只是前几位），内部用
     `git rev-parse --verify <值>^{commit}`解析成完整hash——git原生
     就支持短hash前缀解析，不需要我们自己写模糊匹配逻辑；解析不出来
     就警告+留空，但不会阻止这条产物正常注册。注意Python默认参数
     不能用`None`表示"没传"(会跟"显式传None"混淆)，实现时用一个独立
     的哨兵对象做默认值。
2. `main.py:_main_stages()`里，`generate_standardized_output`返回
   `std_trk_path, meta_path`那一行之后（约1600-1610行附近），调一次
   `register_artifact('ad_perception_tracking', [config['raw_single_video_path']], std_trk_path, {...})`。
   这是本次唯一的产物登记插入点，不需要再动`config_archive.py`或
   `global_config`。

## 附：annotation_batches

这张表是另一位同学直接建表管理的（不在`aerial_backend`的ORM里），已经
有`model_name`/`prompt_version`/`raw_model_response`/`label_revision`
字段，只是`label_revision`涨版本时旧值被覆盖，没有历史。这部分**不采用**
上面`pipeline_artifacts`那套通用方案（代码变化不是这张表输出差异的主要
来源，prompt/model才是，已经有专门字段覆盖），只需要单独加一个DB触发器：
`label_revision`变化时把旧的`model_name`/`prompt_version`/
`raw_model_response`/`labels`/`label_revision`归档进一张
`annotation_batches_history`表。这部分改动完全在`aerial_backend`侧完成，
不涉及那位同学的代码。

## 风险与处理（builder check发现，已补）

1. **路径匹配风险**：`POST /pipeline/artifacts`按`videos.storage_path`
   精确字符串匹配`input_video_paths`，而这个仓库最近5次commit全是在修
   "bindfs路径 vs 直连路径不一致"类问题（`aliyun_scan_dir`/
   `aliyun_download_dir`那一串）。好在`AD_pipeline_1`的
   `database_candidates.py`里`absolute_path`是直接从SQL
   `storage_path AS absolute_path`查出来的（照抄，不是自己扫盘发现的），
   理论上天然一致；但实现前必须拿一条真实的`available_videos.csv`行
   去反查`videos.storage_path`验证确实能匹配上，不能只凭代码推断——
   如果`data_prepare`那个workbench还支持手动录入/浏览别的路径（不走
   这条SQL查询），那条路径就可能跟`storage_path`不是同一个字符串。
2. **422不能跟网络失败一样悄悄吞掉**：只有"查不到网络/后端不可达"才
   按日志记录、不中断pipeline；"路径在`videos`表里查不到"(422)是结构性
   问题（大概率是上面第1点的路径不一致，一旦出现基本是系统性的，会让
   这个功能从第一天起就静默产出零数据），必须用比普通日志更高的级别
   报出来（`logger.error`，有条件的话复用现有`validation_escalation`
   那套飞书升级通知机制），不能跟网络抖动一个处理方式。
3. `annotation_batches`的字段列表以2026-11-16直接在生产库`\d
   annotation_batches`查到的真实结构为准（已在本次对话中查过，有
   `model_name`/`prompt_version`/`raw_model_response`/`label_revision`/
   `review_status`等列）——仓库里`aerial_video_db_schema.sql`里那份
   `CREATE TABLE annotation_batches`定义是旧的、已经跟生产库结构不一致
   了（缺这些列），实现时不要参照那个文件，另外建议单独找时间把这个
   schema dump文件更新一下，不然以后看代码的人会被这份过期定义误导。

## 待办（本次不做，先记录）

- **`upstream_artifact_id`不该要求学生手填**：下一个stage（比如cut-in
  场景提取）很可能不知道自己最初是哪个原始视频，只知道自己读的是哪个
  文件。理想方案：注册接口的输入字段统一叫`input_paths`，学生只填
  "自己实际读的那个文件路径"（不区分是原始视频还是上一个stage的产物），
  后端自动判断——先查`pipeline_artifacts.output_path`，命中就自动继承
  那条记录的`input_video_sha256`并记上`upstream_artifact_id`；查不到
  再查`videos.storage_path`；两边都查不到才422。配套还需要一个
  `GET /pipeline/artifacts?output_path=...`的反查接口。这一条**不在
  本次实现范围内**，`pipeline_artifacts`表结构要为它留好扩展空间
  （`upstream_artifact_id`这列本次就先建上，只是判定逻辑先不做自动化，
  暂时允许为空）。

## 留给实现者决定的细节

- `pipeline_artifacts.output_path`如果是一个目录而非单文件（比如某些
  stage一次产出多个文件），具体怎么表示由实现时定，不强求当前这个
  stage场景之外的情况。
- `register_artifact`的HTTP超时/重试次数，按`urgent_download.py`里
  已有的HTTP调用习惯保持一致即可。
- `pipeline_stages`里这一行的`stage_order`取值，不影响本设计，随手填
  一个合理值。
