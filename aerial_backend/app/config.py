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
    # 下载暂存目录(SSD，不是最终NAS落盘位置，也不在scan_roots扫盘范围内)
    aliyun_download_dir: str = "data"
    # 下载前检查这块SSD剩余空间(GiB)，不够就飞书提示、不下载
    aliyun_min_free_gb: int = 200
    # 连续多少次心跳大小/文件数都没变，才判定"上传完成"(6小时一次心跳，
    # 8次=2天)
    aliyun_stability_scans_required: int = 8
    # 下载单个文件用的超时(秒)，跟list-file这类快调用的60s默认超时分开——
    # 航测视频单个文件常常几GB，60s不够传完(见2026-09-25实际报警)
    aliyun_download_timeout_seconds: int = 1800


settings = Settings()
