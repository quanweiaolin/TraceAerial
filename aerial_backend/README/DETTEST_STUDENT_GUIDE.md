# VRU检测QA模块 —— 学生开发指南

这份文档是接手检测可视化QA这部分工作的开发规范。目的是搞清楚
"哪些代码可以动、哪些不能动、数据从哪来、写到哪去"，避免动到跟你工作无关
的模块，也避免弄乱现有的数据结构。

有任何不确定的地方，先问，不要自己猜着改——尤其是涉及数据库、涉及别的
模块的地方。

---

## 1. 工作范围（可以改的文件）

只允许改这几个文件/目录，其他一律不要动：


| 文件                            | 说明                                                                                                                                                                                                                                                                      |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/services/vru_detection.py` | 核心逻辑：选候选视频、抽帧、跑检测、画框、缩略图、模型对比。**主战场**                                                                                                                                                                                                    |
| `app/api/routers/qa_review.py`  | 审核页面的后端接口 + 前端页面(纯HTML+原生JS，没有额外前端框架/构建工具)                                                                                                                                                                                                   |
| `app/config.py`                 | **只能加`vru_*`开头的新配置项**，不要碰其他已有的配置（数据库、飞书这些）                                                                                                                                                                                                 |
| `app/main.py`                   | **只能改跟VRU相关的部分**（`_run_qa_batch`/`scheduled_qa_batch`/`/qa/run-batch`这几处，代码里有清楚的函数边界），不要碰 `scheduled_scan`/`scheduled_notify`/`scheduled_daily_summary`/`scheduled_metadata_backfill`/`/scan/trigger` 这些，那些是扫盘/通知模块的，不归你管 |
| `.env.example`                  | 只加`AERIAL_VRU_*` 这几行，其他不要动                                                                                                                                                                                                                                     |
| `pyproject.toml`                | 只加VRU相关的新依赖（比如以后MLflow client库），加完看第5节怎么处理`uv.lock`                                                                                                                                                                                              |

## 2. 绝对不要碰的东西（及原因）


| 东西                                                               | 为什么不能碰                                                                                                            |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `app/services/scanner.py`                                          | 扫盘核心逻辑，全系统视频入库都靠它，改坏了整个数据链路断掉                                                              |
| `app/services/notifier.py`（除了你自己新增`event_type`模板那一行） | 飞书通知基础设施，别的功能也在用                                                                                        |
| `app/services/backfill.py`                                         | 老视频元数据回填，跟你的任务无关                                                                                        |
| `app/models.py`                                                    | 数据库ORM定义，改列/加表要经过设计确认，不能自己加                                                                      |
| `app/db.py` / `app/api/routers/videos.py`                          | 跟你的任务无关                                                                                                          |
| 任何`.sql` 文件（`aerial_video_db_schema.sql` 等）                 | 数据库schema变更必须走确认流程，不能自己改表结构                                                                        |
| 生产数据库里的表                                                   | 你的代码只允许**读** `videos` 表（`select_candidate_videos`已经封装好了，直接用，不要自己写新的SQL插入/更新`videos`表） |

## 3. 数据来源和写入规范

### 3.1 原始视频（Raw）—— 只读，永远不要写/删

```
/mnt/nas/ADSafety/ADSafety/Aerial_Raw_Videos/...
/mnt/Expansion/data/...
```

这是航测原始素材，任何情况下你的代码都不能对这两个目录下的文件做写入、
重命名、删除操作。读取（`cv2.VideoCapture`打开视频）没问题。

### 3.2 处理结果（Processing_Data）—— Test文件夹下可以存下原始图片+检测图片

```
/mnt/nas/ADSafety/ADSafety/Aerial_Processing_Data/...
```

正式环境是这个NAS路径，本地开发时可以在 `.env` 里把 `AERIAL_VRU_PROCESSING_ROOT`
指向本地一个目录（比如项目根下的 `data/`，已经在 `.gitignore` 里）——**必须填
绝对路径**，不要填相对路径。

对应的Processing路径怎么从Raw路径算出来，看 `compute_processing_dir()`，
规则已经定好了不要改：把Raw根目录换成Processing_Data根目录，中间目录原样
保留，视频文件名(去掉扩展名)变成一个新文件夹。

每个视频的产出全部放在它自己的 `.../<视频名>/Test/` 目录下，命名规则：

```
raw_f{帧号}.jpg                                原始抽帧图，不能改名规则
raw_f{帧号}_{model_name}_{model_version}.jpg   检测叠加图
raw_f{帧号}_{model_name}_{model_version}_review.json   人工审核结果
```

这套命名规则前端、后端好几处代码都依赖它，**不要为了图方便改文件名格式**，
真要改必须同时改 `vru_detection.py` 里所有拼接/解析文件名的地方（用
`grep -rn "raw_f{" app/` 搜一遍确认改全）。

### 3.3 批次清单（`_qa_batches/`）

```
/mnt/nas/ADSafety/ADSafety/Aerial_Processing_Data/_qa_batches/<日期>.json
```

前端靠这个知道"某一天跑了哪些"，不用扫全量NAS目录。这个文件是**当次运行
完整覆盖写入**的（不是追加），如果你要改这块逻辑，想清楚"同一天多次触发
会不会互相覆盖"这个问题，目前的设计默认同一天只会有一次真正有效的批次
（已经处理过的视频会被去重跳过，不会重复生成）。

### 3.4 `videos` 表（Postgres）—— 只读

```python
from app.services.vru_detection import select_candidate_videos
candidates = await select_candidate_videos(session, collect_date=..., limit=...)
```

这个函数已经封装好了怎么查候选视频，直接用。**不要自己写SQL去
INSERT/UPDATE/DELETE `videos` 表**——那张表是扫盘服务(`scanner.py`)独占写入的，
你的代码只应该读它。

### 3.5 模型文件

```
checkpoints/*.pt   （.gitignore已排除，不进仓库）
```

`AERIAL_VRU_MODEL_PATH` 指向"批次默认用哪个模型"，`list_available_models()`
会扫这个目录下所有`.pt`文件给前端下拉框选。新模型文件直接放进这个目录，
不用改代码、不用重启服务就能在下拉框里看到。

## 4. 现有设计里"故意留的坑"，你需要知道

- **`model_version="manual"`是占位符**：现在"对比模型"功能产出的文件都写死
  `model_version="manual"`，因为还没有MLflow这种正式的模型版本管理。等以后
  接入MLflow，这里要改成真正的版本号，届时`AERIAL_VRU_MODEL_NAME`/`_VERSION`
  这两个配置项的意义也会变——现在不用纠结这个，先用文件名当标识就行。
- **`AERIAL_VRU_MODEL_NAME`(人起的名字)和模型文件名不是一回事**：批次默认结果
  按`.env`里配置的名字命名，"对比模型"功能按文件名命名，两者不一定一致，
  已知这是个容易踩坑的地方（`get_default_model_filename()`是专门为了处理这个
  不一致加的兼容逻辑，改代码时留意这块）。
- **随机抽帧的种子固定(`SEED=2025`)是有意为之**：为了能复现学生(你之前那位)
  已经跑过的结果，不要改成"每个视频独立种子"这种看起来更严谨的写法。
- **原图分辨率不能随便改小**：检测/推理都是在原始分辨率上跑的，只有专门给
  浏览器看的缩略图(`get_or_create_thumbnail`)会缩小，这是特意分开的两条路径，
  别为了"页面加载快"就把原图也缩小了，会影响检测精度和"同一帧对比不同模型"
  这个能力的一致性。

## 5. 代码规范

- **重依赖(torch/ultralytics/opencv)只能局部import**，不要挪到文件顶部——
  这几个包很重，模块顶部import的话，任何import这个文件的代码(比如只是想
  用`select_candidate_videos`查候选)都会被迫要求装好这些依赖。
- **模型推理/图像处理这类阻塞操作，暴露给FastAPI的地方要丢进线程池**：

  ```python
  await asyncio.to_thread(某个同步阻塞函数, 参数...)
  ```

  不这么做会卡住整个服务的事件循环，其他所有请求/定时任务都会被拖慢。
  参考现有代码里 `run_detection_batch`/`detect_with_model_on_frame` 的调用方式。
- **复用现成的函数，不要重复造轮子**：比如画框统一用 `_draw_detections`，
  路径映射统一用 `compute_processing_dir`，别自己再写一套。
- **改动 `app/main.py`/`app/config.py` 这种共享文件时，只加不改**：新增你需要的
  内容，不要顺手"顺便优化"一下别的无关代码。

## 6. 依赖/环境管理

- `pyproject.toml` 加新依赖后，`uv.lock` 需要**在能访问真实GPU/CUDA环境的那台
  机器上重新生成**（`uv lock`），生成后直接commit，其他机器`uv sync`装，不要
  在自己电脑上各自生成`uv.lock`再提交，会导致锁文件冲突/不一致。
- 不要擅自升级/降级 `torch`/`torchvision`/`ultralytics` 的版本，这几个是配合
  生产环境的CUDA版本锁定的，要改先确认。

## 7. 提交规范

- 遇到任何"要不要新建数据库表"、"要不要改现有函数签名"、"这个设计是不是该
  这样"的问题，先问，不要自己决定。
- commit信息写清楚改了什么、为什么改，参考仓库里已有的commit记录风格。
- 涉及生产环境配置（`.env`真实值、NAS路径权限）的事情不要自己处理，找我们。

## 8. 怎么使用和测试

**⚠️ FastAPI服务必须启动着，才能测接口/测审核页面。** 下面第8.2节往后的每一步，
只要涉及 `curl`、浏览器打开页面，前提都是服务在跑。第8.1节（脱离服务的纯脚本
验证）例外，不需要服务启动。

### 8.1 启动服务

```bash
cd aerial_backend
uv sync                        # 第一次跑/依赖有更新时执行
uv run uvicorn app.main:app --host 0.0.0.0 --port 8088
```

本地临时调试可以就这样前台跑着（看得到实时日志，方便调试）。如果要让它
长期挂着不随终端关闭而退出，用 `tmux`：

```bash
tmux new -s aerial
cd aerial_backend
uv run uvicorn app.main:app --host 0.0.0.0 --port 8088
# 看到 "Uvicorn running on http://0.0.0.0:8088" 之后
# 按 Ctrl+B 松开再按 D，分离session（不要直接关窗口/Ctrl+C，那样进程会被杀掉）
```

改完代码要重新测，`Ctrl+C` 停掉旧进程，重新执行 `uv run uvicorn ...` 才会生效
（改Python代码不会热加载，除非加 `--reload` 参数，仅本地调试用，生产环境不要
用 `--reload`）。

### 8.2 确认服务活着

```bash
curl http://127.0.0.1:8088/qa/models
```

能返回一段JSON（模型文件列表）说明服务正常。如果报 `curl: (7) Failed to
connect`，说明服务根本没启动，回第8.1节先把服务跑起来，别的都别测。

### 8.3 分层测试，从小到大

**第一层：不碰数据库/不碰服务，纯验证模型本身能跑**（这一步不需要服务启动）：

```bash
uv run python -c "
import cv2
from ultralytics import YOLO
import torch

print('CUDA可用:', torch.cuda.is_available())
model = YOLO('checkpoints/best_vru_universal.pt')
print('模型类别:', model.names)
print('任务类型:', model.task)
"
```

出错就在这一层解决（CUDA探测失败、模型文件路径不对等），不要带着数据库/
服务一起排查。

**第二层：直接脚本调用核心函数，跳过HTTP，看真实报错栈**（这一步也不需要
服务启动，因为是直接跑Python脚本，不经过FastAPI）：

```bash
uv run python -c "
import asyncio
from app.db import SessionContext
from app.services.vru_detection import select_candidate_videos, run_detection_batch

async def main():
    async with SessionContext() as session:
        candidates = await select_candidate_videos(session, only_latest_date=True, limit=2)
        print(f'候选视频数: {len(candidates)}')
        stats = run_detection_batch(
            candidates,
            model_path='checkpoints/best_vru_universal.pt',
            model_name='vru_universal',
            model_version='v1',
        )
        print(stats)

asyncio.run(main())
"
```

跑完去 `stats` 里打印出来的 `test_dir` 路径下 `ls` 一下，确认图片真的生成了。

**第三层：走真实的HTTP接口，这一步开始必须先启动服务**（见8.1）：

```bash
# 小范围测试：只测某一天的前N个视频，notify=false避免刷飞书群
curl -X POST "http://127.0.0.1:8088/qa/run-batch?collect_date=2026-08-01&limit=10&notify=false"
```

返回 `{"processed": N, "failed": [...], "results": [...]}`。**注意**：如果这批
视频之前已经成功跑过一次，`processed` 会是 `0`——这是设计如此（已经有`Test`
文件夹的视频不会重复处理），不是bug，不用慌。

### 8.4 打开审核页面看结果（必须服务在跑）

浏览器直接打开：

```
http://<服务器地址>:8088/qa/review
```

前提是第8.3节第三层已经成功跑出过至少一批结果，不然页面是空的（"批次"
下拉框没有可选日期）。

### 8.5 常见问题排查

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `curl: (7) Failed to connect` | 服务没启动/被杀了 | 回8.1重新启动 |
| `/qa/run-batch` 返回 `{"processed":0,...}` | 幂等设计：这批视频已经有`Test`文件夹了，不是bug | 换一批没处理过的视频测，或者确认这本来就是预期结果 |
| `/qa/run-batch` 返回 `{"skipped": "vru_model_path not configured"}` | `.env`里`AERIAL_VRU_MODEL_PATH`是空的 | 配置这个值 |
| 审核页面图片一直显示"该模型尚未检测过此帧" | 要么是真的没检测过（点"点击检测"就行），要么是选的模型刚好是`.env`里配置的默认模型但下拉框没显示"当前默认"标记(版本对不上) | 确认选的是不是下拉框里标了"← 当前`AERIAL_VRU_MODEL_PATH`"那一项 |
| 页面图片加载很慢/加载不出来 | 原图是4K大图，直接传给浏览器在远程/弱网环境下容易超时 | 确认走的是`/qa/thumbnail`不是`/qa/image`；第一次加载某张图会现算缩略图有点慢，属于正常，刷新一次就快了(已缓存) |
| 改了`vru_detection.py`/`qa_review.py`代码，重启后行为没变 | 忘了真的重启进程，或者用了`--reload`但没触发 | `Ctrl+C`杀掉旧进程，重新`uv run uvicorn ...`确认启动日志时间戳是最新的 |
