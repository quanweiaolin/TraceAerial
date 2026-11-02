"""
全局配置。所有敏感信息(数据库密码、飞书webhook)走环境变量/.env，
不要硬编码进代码，方便学生本地跑的时候和线上环境分开。
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AERIAL_", extra="ignore")

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
    # videos.location_id(locations表记录了当时生效的阈值，供追溯)
    address_cluster_max_diameter_meters: float = 20.0

    # 飞行稳定性：单个视频自己的SRT轨迹里，任意两帧的GPS+高度总位移超过
    # 这个值(米)就判定is_stable=False——这组视频假设拍摄期间飞机基本停在
    # 同一个点(地址聚类/下游SIFT对齐都依赖这个假设)，位移太大说明飘了/
    # 飞手手动飞去了别的地方，这条视频的单点GPS/地址可能不准
    flight_stability_max_drift_meters: float = 30.0

    # LLM agent(飞书问答机器人，app/llm_agent/)：这3个走标准ANTHROPIC_*
    # 变量名(不带AERIAL_前缀)，因为换Provider/配置时希望跟标准Anthropic
    # SDK文档/生态的叫法保持一致，不要求用户额外记一套改了名字的变量。
    # 用validation_alias绕开env_prefix，不靠Anthropic SDK自己探测os.environ
    # ——SDK的探测只在它自己new出来的进程环境里才生效，换一种启动方式
    # (systemd EnvironmentFile vs 直接uvicorn/uv run)能不能读到不确定，
    # 走我们自己的settings跟其它配置项路径一致，更可控。
    anthropic_auth_token: str = Field(default="", validation_alias="ANTHROPIC_AUTH_TOKEN")
    anthropic_base_url: str = Field(default="", validation_alias="ANTHROPIC_BASE_URL")
    anthropic_model: str = Field(default="", validation_alias="ANTHROPIC_MODEL")

    # 飞书LLM问答机器人(app/services/feishu_bot.py)：一个新建的企业自建应用，
    # 跟上面feishu_webhook_url那个只能单向发消息的自定义机器人是两个完全
    # 不同的东西，不要混用。长连接模式不需要Verification Token/Encrypt Key。
    feishu_llm_app_id: str = Field(default="", validation_alias="LLM_Feishu_AppId")
    feishu_llm_app_secret: str = Field(default="", validation_alias="LLM_Feishu_AppSecret")


settings = Settings()
