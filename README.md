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


### 1. shell（psql）

```bash
psql "postgresql://ad4che:ad4che@localhost:5432/aerial_db"
```

```sql
SET search_path TO aerial, public;   -- 进去先执行这行，之后查表不用加前缀
\dt                                   -- 列出所有表
\d videos                             -- 看videos表结构
SELECT * FROM videos LIMIT 5;
```

一次性查询（不进交互模式）：

```bash
psql "postgresql://ad4che:ad4che@localhost:5432/aerial_db" \
  -c "SET search_path TO aerial, public;" \
  -c "SELECT storage_path, created_at FROM videos ORDER BY created_at DESC LIMIT 5;"
```

### 2. Python

```python
import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host="localhost", port=5432, user="ad4che", password="ad4che", database="aerial_db",
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

具体访问地址/账号找项目负责人要，这里不写死（不同环境部署的地址不一样）。

### `videos` 表字段说明

一个视频文件一行，`sha256` 是内容哈希，同一份内容（哪怕换了路径/文件名）
只会有一条记录。


| 字段                          | 含义                                                                                                          |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `sha256`                      | 视频内容的sha256哈希（32字节），主键，用于内容去重                                                            |
| `original_name`               | 原始文件名                                                                                                    |
| `storage_path`                | 视频在服务器/NAS上的绝对路径                                                                                  |
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
