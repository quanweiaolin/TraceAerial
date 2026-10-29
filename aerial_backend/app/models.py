"""
ORM模型，一一对应 aerial_video_db_schema.sql 里的表。
这里先写核心链路(videos / pipeline / scan / notification / submission)，
annotation_batches / inference_runs / tracking_runs / generated_datasets /
scene_events / map_datasets / highd_datasets 按同样模式加，
字段名和SQL文件保持一致，不要在ORM里改名字，减少心智负担。
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, SmallInteger,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class StageStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    blocked = "blocked"


class Operator(Base):
    __tablename__ = "operators"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(32))
    dept: Mapped[str | None] = mapped_column(String(64))
    
class Device(Base):
    __tablename__ = "devices"
    device_id: Mapped[int] = mapped_column(primary_key=True)
    device_code: Mapped[str] = mapped_column(String(64), unique=True)
    model: Mapped[str | None] = mapped_column(String(64))
    sensor_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    owner_unit: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Video(Base):
    __tablename__ = "videos"
    # 数据库里是BYTEA(32字节)；Python侧交互时统一用hex字符串更方便，
    # 具体转换见 services/hashing.py 的 sha256_hex_to_bytes/bytes_to_hex
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    # 2026-10-28加：阿里云PDS返回的content_hash字段本身就是sha1(不是sha256，
    # 两者算法不同、不能互相比对)，专门给aliyun_sync下载前查重用("这份内容
    # 是不是已经在我们系统里了，不管它现在实际存在哪个路径")，不是内容身份
    # 的主键，sha256仍然是。只在扫盘第一次真正读文件算哈希时顺带算出来
    # (services/hashing.py的compute_sha256_and_sha1)，历史数据和已经被瘦身
    # 替换、原始字节已经不在的旧记录，这一列会是NULL，物理上补不出来。
    sha1: Mapped[bytes | None] = mapped_column(LargeBinary(20))
    original_name: Mapped[str] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_seconds: Mapped[float | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    fps: Mapped[float | None]
    codec: Mapped[str | None] = mapped_column(String(32))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.device_id"))
    # DB里是TIMESTAMPTZ，这里显式声明timezone=True，否则asyncpg绑定带时区的
    # datetime(比如extract_new_video_fields返回的collected_at)时会报类型不匹配
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_by: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    collect_lon: Mapped[float | None]
    collect_lat: Mapped[float | None]
    # 2026-10-29加：地址聚类(给下游多视频SIFT对齐用)。city/flight_height从
    # storage_path里符合会话命名规范(session_naming.SESSION_FOLDER_RE)那段
    # 直接读；site是聚类后的最终地址标签(如panshiroad_1，不是正则里裸的
    # site分组值)，一旦分配给某个视频就不可再改，只有符合命名规范+有GPS
    # 坐标(collect_lon/lat)的视频才会被分配，见scanner.py的
    # _apply_session_metadata/_assign_city_site。srt_height是SRT遥测里
    # 实际记录的高度(rel_alt，相对起飞点)，跟flight_height(文件夹名里的
    # 标称高度)分开存，聚类时height维度优先用srt_height，没有才退回
    # flight_height
    city: Mapped[str | None] = mapped_column(Text)
    site: Mapped[str | None] = mapped_column(Text)
    flight_height: Mapped[int | None]
    srt_height: Mapped[float | None]
    # 2026-10-29加：SRT整段轨迹里任意两帧GPS+高度的总位移超过
    # AERIAL_FLIGHT_STABILITY_MAX_DRIFT_METERS就是False——地址聚类假设
    # 拍摄期间飞机基本停在同一点，位移太大这条视频的单点GPS可能不代表
    # 真实拍摄位置。没有SRT/解析不出轨迹时是NULL(未知，不是True也不是
    # False)，不是所有视频都能判定
    is_stable: Mapped[bool | None] = mapped_column(Boolean)
    # 同一个storage_path上的视频被重新编码(比如瘦身转码)会产生新sha256；
    # 这个自引用字段指回被替代的旧sha256，旧记录靠deleted_at软删除下线，
    # 保留血缘方便追溯"这个位置的视频历史上有过哪些版本"。见scanner.py处理逻辑。
    replaces_sha256: Mapped[bytes | None] = mapped_column(
        LargeBinary(32), ForeignKey("videos.sha256")
    )
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pipeline_status: Mapped[list["VideoPipelineStatus"]] = relationship(
        back_populates="video"
    )


class Location(Base):
    """GPS聚类得到的"地址"注册表：只记录地址标签本身+聚类时生效的阈值+
    创建时间，不存中心点坐标——判断一个新视频是否该归到某个已有地址时，
    领导者算法要求"跟簇内所有已有成员比距离"，而这些成员本身就是videos
    表里已经打上同一个(city, site)标签的记录，不用在这里重复存一份坐标
    (见scanner.py的_assign_city_site)。地址一旦分配给某个视频就不可再
    改(immutable)，新物理地址只会新增一行，不会合并/删除已有的。"""

    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("city", "site"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    city: Mapped[str] = mapped_column(Text)
    site: Mapped[str] = mapped_column(Text)
    threshold_meters: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name_cn: Mapped[str] = mapped_column(String(64))
    stage_order: Mapped[int]
    is_active: Mapped[bool] = mapped_column(default=True)
    description: Mapped[str | None] = mapped_column(Text)


class VideoPipelineStatus(Base):
    __tablename__ = "video_pipeline_status"
    __table_args__ = (UniqueConstraint("video_sha256", "stage_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    video_sha256: Mapped[bytes] = mapped_column(
        LargeBinary(32), ForeignKey("videos.sha256", ondelete="CASCADE")
    )
    stage_id: Mapped[int] = mapped_column(ForeignKey("pipeline_stages.id"))
    status: Mapped[StageStatus] = mapped_column(default=StageStatus.pending)
    progress_percent: Mapped[int] = mapped_column(SmallInteger, default=0)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    ref_table: Mapped[str | None] = mapped_column(String(64))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    video: Mapped["Video"] = relationship(back_populates="pipeline_status")
    stage: Mapped["PipelineStage"] = relationship()


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    root_path: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    files_found: Mapped[int] = mapped_column(default=0)
    files_new: Mapped[int] = mapped_column(default=0)
    files_duplicate: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="running")
    error_message: Mapped[str | None] = mapped_column(Text)


class ScanDiscovery(Base):
    __tablename__ = "scan_discoveries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scan_job_id: Mapped[int] = mapped_column(
        ForeignKey("scan_jobs.id", ondelete="CASCADE")
    )
    file_path: Mapped[str] = mapped_column(Text)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    hash_status: Mapped[str] = mapped_column(String(16), default="pending")
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    resolved_video_sha256: Mapped[bytes | None] = mapped_column(
        LargeBinary(32), ForeignKey("videos.sha256")
    )
    is_new_task: Mapped[bool | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    video_sha256: Mapped[bytes | None] = mapped_column(
        LargeBinary(32), ForeignKey("videos.sha256")
    )
    stage_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_stages.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    target_operator_id: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    channel: Mapped[str] = mapped_column(String(16), default="feishu")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    retry_count: Mapped[int] = mapped_column(default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataSubmission(Base):
    __tablename__ = "data_submissions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    submitted_by: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    target_table: Mapped[str] = mapped_column(String(64))
    target_ref_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    validation_status: Mapped[str] = mapped_column(String(16))
    validation_errors: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AliyunSyncEvent(Base):
    """阿里云PDS自动下载流程的事件记录，只在"真的有事发生"时才INSERT
    (文件夹判定上传完成/检测到异常)，日常巡检发现无变化不写这张表。
    见 add_aliyun_sync_events.sql 顶部注释。"""

    __tablename__ = "aliyun_sync_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32))
    issue_type: Mapped[str | None] = mapped_column(String(32))
    cloud_folder_id: Mapped[str] = mapped_column(Text)
    cloud_folder_path: Mapped[str | None] = mapped_column(Text)
    videos: Mapped[list] = mapped_column(JSONB, default=list)
    scan_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AliyunFolderScan(Base):
    """阿里云PDS文件夹心跳历史，每次定时扫描每个被监控文件夹记一行，永久保留。
    只在changed=True时填充files完整文件列表，见 add_aliyun_folder_scans.sql。"""

    __tablename__ = "aliyun_folder_scans"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    drive_id: Mapped[str] = mapped_column(Text)
    cloud_folder_id: Mapped[str] = mapped_column(Text)
    cloud_folder_path: Mapped[str | None] = mapped_column(Text)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    file_count: Mapped[int] = mapped_column(Integer)
    changed: Mapped[bool] = mapped_column(Boolean, default=False)
    files: Mapped[list | None] = mapped_column(JSONB)
