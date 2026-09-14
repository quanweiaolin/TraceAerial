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

    # VRU检测QA可视化用的模型，依赖版本确认前留空——留空时定时/手动任务会跳过
    # 不报错，见 services/vru_detection.py
    vru_model_path: str = ""
    vru_model_name: str = ""
    vru_model_version: str = ""
    # 检测结果(Test/目录、批次清单)存放的根目录，正式环境是NAS的
    # Aerial_Processing_Data；NAS权限没弄好之前可以先指向本地目录测试功能，
    # 必须填绝对路径(比如项目根目录下的data/，填它的绝对路径，不要填相对路径
    # 比如"../data"——图片是后续通过HTTP接口按路径读取的，相对路径依赖进程
    # 当时的工作目录，容易出问题)，权限好了改回NAS绝对路径、重启服务即可
    vru_processing_root: str = "/mnt/nas/ADSafety/ADSafety/Aerial_Processing_Data"

    # 数据库连接池
    db_pool_size: int = 10
    db_max_overflow: int = 10


settings = Settings()
