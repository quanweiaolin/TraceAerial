*[English version](README.en.md)*

# 航测数据管理系统

管理航测视频从采集、标注、推理、追踪到生成数据集、场景提取的全流程，解决此前依赖口头沟通、
进度不可见、数据来源不可追溯的问题。

## 现状


| 模块                                                    | 状态                          |
| ------------------------------------------------------- | ----------------------------- |
| 数据库设计                                              | ✅ 已完成，视频线全流程覆盖   |
| 自动扫盘                                                | ✅ 已实现                     |
| 飞书任务提醒                                            | ⬜ 未实现队列模式             |
| 数据提交校验 + 失败升级提醒                             | 🟡 骨架已定，逐类补充中       |
| 各阶段资产的 ORM/接口（标注/推理/追踪/数据集/场景提取） | 🟡 骨架已定，逐类补充中       |
| 地图数据线                                              | ⬜ 暂缓，机制已预留，阶段待定 |
| 前端                                                    | ⬜ 未开始，按计划先做后端     |

## 这套系统解决什么问题

- **进度可见**：每份视频在哪个阶段、谁在处理、什么时候完成，一处可查，不用口头问
- **权限清晰**：不同角色（管理员/标注员/飞手等）权限分开，不共用账号
- **异常自动升级**：数据提交连续校验失败会自动通知管理员，不需要人工盯着
- **数据不重不丢**：每份视频用内容哈希去重，处理链路上每一步都有记录可追溯

## 技术栈

- PostgreSQL 14+（`aerial` schema）
- Python + FastAPI + SQLAlchemy（异步）+ APScheduler（定时任务）
- 飞书自定义机器人 webhook 做通知

## 数据库调用方式

表建在 `aerial` 这个schema下（不是默认的 `public`），下面几种方式都要注意这点。

学生/外部人员统一用只读账户 `reader`（密码找项目负责人要，不写进代码库），只能
查询、不能改数据，下面的例子都用这个账户。不要用管理员账户连库。

### 1. shell（psql）

```bash
psql "postgresql://reader:<password>@localhost:5432/aerial_db"
```

```sql
SET search_path TO aerial, public;   -- 进去先执行这行，之后查表不用加前缀
\dt                                   -- 列出所有表
\d videos                             -- 看videos表结构
SELECT * FROM videos LIMIT 5;
```

一次性查询（不进交互模式）：

```bash
psql "postgresql://reader:<password>@localhost:5432/aerial_db" \
  -c "SET search_path TO aerial, public;" \
  -c "SELECT storage_path, created_at FROM videos ORDER BY created_at DESC LIMIT 5;"
```

### 2. Python

```python
import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host="localhost", port=5432, user="reader", password="<password>", database="aerial_db",
        server_settings={"search_path": "aerial,public"},
    )
    rows = await conn.fetch("SELECT storage_path, created_at FROM videos LIMIT 5;")
    for r in rows:
        print(dict(r))
    await conn.close()

asyncio.run(main())
```

常用查询示例（把上面 `conn.fetch(...)` 里的SQL换成下面这两条即可）：

```sql
-- 1. 时长小于5秒的视频
SELECT storage_path, duration_seconds FROM videos WHERE duration_seconds < 5;

-- 2. 缺经纬度的视频（经度、纬度任意一个为空就算）
SELECT storage_path FROM videos WHERE collect_lon IS NULL OR collect_lat IS NULL;
```

需要装依赖：`pip install asyncpg`

### 3. Metabase（不写代码，网页里点选/写SQL做查询和看板）

[Metabase](https://www.metabase.com/) 是一个开源BI工具，连上数据库之后可以在网页里
拖拽建图表、写SQL做即席查询，不用写Python/psql，适合不想碰代码、只想看数据的场景。
连接这个库时同样要注意：默认schema是 `public`，Metabase里选数据库/表的时候要选
`aerial` 这个schema下的表，或者在连接的高级设置里把 `public` 改成 `aerial`。

具体访问地址：服务器IP:3000, 账号密码找quanwei/ziyu要。

### `videos` 表字段说明

一个视频文件一行，`sha256` 是内容哈希，同一份内容（哪怕换了路径/文件名）
只会有一条记录。


| 字段                          | 含义                                                                                                          |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `sha256`                      | 视频内容的sha256哈希（32字节），主键，用于内容去重                                                            |
| `original_name`               | 原始文件名                                                                                                    |
| `storage_path`                | 视频在服务器/NAS上的绝对路径                                                                                  |

> `storage_path`如果是`/mnt/Expansion2-shared/...`开头：这是走bindfs的路径，专门给阿里云下载流程用的(下载工具要chmod，exFAT不支持chmod，bindfs负责吞掉这个调用)，跟"读文件"没关系，但读的时候照样要绕一圈FUSE——实测同一份数据直连读能到2.9GB/s，走这条路径只有2.8MB/s，差了1000倍。**人工查看/拷贝数据时，把`Expansion2-shared`换成`Expansion2`**，内容完全一样、快得多：`path.replace("Expansion2-shared", "Expansion2")`，或者直接用`aerial_backend/scripts/fast_path.py`(支持更多这类映射，配置在不提交进git的`path_aliases.json`里，第一次用先`cp path_aliases.json.example path_aliases.json`)。
| `file_size_bytes`             | 文件大小（字节）                                                                                              |
| `duration_seconds`            | 视频时长（秒）                                                                                                |
| `width` / `height`            | 分辨率                                                                                                        |
| `fps`                         | 帧率                                                                                                          |
| `codec`                       | 编码格式（如 h264、hevc）                                                                                     |
| `collected_at`                | 视频采集时间（优先取视频自带的创建时间元数据，没有则用文件修改时间）                                          |
| `collect_lon` / `collect_lat` | 采集地点经纬度（来自同名SRT遥测字幕文件，没有SRT文件时为空）                                                  |
| `replaces_sha256`             | 如果这个视频是"同一位置的旧视频被重新编码/压缩"产生的新版本，这里指向被替代的旧`sha256`；普通新视频这一列为空 |
| `created_at` / `updated_at`   | 这一行记录本身的创建/更新时间（不是视频拍摄时间）                                                             |
| `deleted_at`                  | 非空代表这条记录已下线（通常是因为同一位置的视频被替换成了新版本）                                            |

**瘦身转码(重新编码)和挪动位置，不要在同一次操作里合并做**：扫盘能正确
识别这两种情况，靠的分别是"内容不变、路径变了"（挪动）和"路径不变、内容
变了"（瘦身）——两者都只认各自那一个精确匹配的信号。如果一个文件同时被
挪了位置又瘦身了（新路径+新内容），系统没有办法可靠判断新文件对应的是
哪一条旧记录（文件名/大小这类特征去猜是不可靠的启发式，猜错比不关联更糟），
结果是：旧记录永远留着不会下线，新文件被当成完全无关的新视频入库，丢失
`replaces_sha256`血缘关系。正确做法是分两步：先把文件挪到新位置（内容不变，
系统会自动识别成"整体移动"并发通知），确认挪动生效之后，再在新位置原地
转码瘦身。

## 紧急下载工具（跳过稳定期，立刻下载+入库）

正常情况下，阿里云新上传的文件夹要等"连续多次心跳没变化"（通常几天）才会被
系统认为"上传完了"、自动下载。如果有紧急需求（比如要给某个客户马上处理某个
文件夹的数据），不想等这几天，用这个脚本：

```bash
cd aerial_backend
uv run python scripts/urgent_download.py \
    --session-name shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104
```

`--session-name`必须是完整的会话文件夹名（格式`city_site_drone_height_
person_date`，机型只认`app/services/session_naming.py`里`SESSION_FOLDER_
DEVICES`那个白名单，不在名单里需要先找管理员加进去），而且这个文件夹必须
在云端drive的**根目录**下（不能嵌套在别的目录里）——这两条有任何一条不满足，
命令会直接在终端报错退出，不会下载、不会发任何消息。

校验通过后，命令会立刻把控制权还给终端（不用自己加`&`或者`nohup`），真正
的下载在后台独立进行。完成或者失败都会发一条飞书消息到群里；如果长时间
（比如半小时以上）没收到任何消息，直接重新执行一遍同样的命令就行——这个
脚本设计成可以安全重复执行，已经下载好的文件不会重新传一遍，已经入库的
视频也不会出现在数据库里两遍。

不想SSH上服务器的话，也可以直接发HTTP请求，效果完全一样（同一套校验+
同一个后台进程）：

```bash
curl -X POST "http://<服务器地址>:8088/aliyun/urgent-download?session_name=shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104"
```

格式不对会直接返回422，说明哪里错了；云端根目录下找不到这个文件夹返回404；
校验通过返回200，响应体里有后台日志路径，真正的结果还是看飞书消息。

**这个工具跳过的是"稳定期等待"这一道保险**，意味着可能抓到一份云端还没
传完的数据——这是"紧急"模式主动接受的代价，不是bug，没有别的办法规避。
如果云端之后又有新文件传上来，常规的自动化流程会检测到并发飞书提醒，但
没法保证第一次抓到的这一份就是完整的。
