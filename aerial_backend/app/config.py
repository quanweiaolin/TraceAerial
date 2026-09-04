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


settings = Settings()
