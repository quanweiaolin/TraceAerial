"""
全局配置。所有敏感信息(数据库密码、飞书webhook)走环境变量/.env，
不要硬编码进代码，方便学生本地跑的时候和线上环境分开。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AERIAL_")

    # postgresql+asyncpg://user:pass@host:5432/dbname
    database_url: str

    # 飞书自定义机器人webhook地址；也可以换成飞书开放平台的应用+access_token方式
    feishu_webhook_url: str = ""
    # 机器人开了"签名校验"才需要填，不开就留空；见 services/notifier.py 的签名逻辑
    feishu_webhook_secret: str = ""

    # 校验连续失败多少次后升级提醒 ziyu/quanwei
    validation_fail_escalation_threshold: int = 3
    # 升级提醒对象的 operator.username 列表
    escalation_usernames: list[str] = ["ziyu", "quanwei"]

    # 扫盘根目录，多个用逗号分隔，也可以在触发扫盘接口时临时指定
    scan_roots: list[str] = []

    # 数据库连接池
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # 阿里云PDS自动下载：官方aliyun CLI二进制路径，留空则默认用
    # extern/pds-drive-sync/.tools/aliyun(pds-sync setup装的那个)
    aliyun_cli_binary: str = ""
    # PDS哪几个空间要扫，跟pds-drive-sync自己的默认一致
    aliyun_spaces: list[str] = ["personal", "team", "enterprise"]
    # 下载落地根目录。默认是repo下的data/(本地测试用的SSD暂存目录)，
    # 生产环境在.env里配成真实的NAS路径(比如/mnt/Expansion2/Aerial_Raw_Videos)，
    # 下载完成后会自动scanner.run_scan扫描进videos库，见services/aliyun_sync.py
    aliyun_download_dir: str = "data"
    # 下载前检查这个目录所在盘的剩余空间(GiB)，不够就飞书提示、不下载
    aliyun_min_free_gb: int = 200
    # 连续多少次心跳大小/文件数都没变，才判定"上传完成"(6小时一次心跳，
    # 8次=2天)
    aliyun_stability_scans_required: int = 8
    # 下载单个文件用的超时(秒)，跟list-file这类快调用的60s默认超时分开——
    # 航测视频单个文件常常几GB，60s不够传完(见2026-09-25实际报警)
    aliyun_download_timeout_seconds: int = 1800

    # 地址聚类(GPS+高度)：同一个地址(locations表一行)内任意两点最大距离(米)，
    # 下游要拿同地址的多个视频做SIFT对齐/稳像，20m是为了控制对齐后黑边大小
    # (2026-10-29敲定)。只影响新建地址的判定，改这个值不会动已经分配好的
    # videos.site(locations表记录了当时生效的阈值，供追溯)
    address_cluster_max_diameter_meters: float = 20.0


settings = Settings()
