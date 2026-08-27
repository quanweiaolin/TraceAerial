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
    BigInteger, ForeignKey, LargeBinary, SmallInteger, String, Text,
    UniqueConstraint, func,
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


class Video(Base):
    __tablename__ = "videos"
    # 数据库里是BYTEA(32字节)；Python侧交互时统一用hex字符串更方便，
    # 具体转换见 services/hashing.py 的 sha256_hex_to_bytes/bytes_to_hex
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    original_name: Mapped[str] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_seconds: Mapped[float | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    fps: Mapped[float | None]
    codec: Mapped[str | None] = mapped_column(String(32))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.device_id"))
    collected_at: Mapped[datetime | None]
    collected_by: Mapped[int | None] = mapped_column(ForeignKey("operators.id"))
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    deleted_at: Mapped[datetime | None]

    pipeline_status: Mapped[list["VideoPipelineStatus"]] = relationship(
        back_populates="video"
    )


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
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    ref_table: Mapped[str | None] = mapped_column(String(64))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    video: Mapped["Video"] = relationship(back_populates="pipeline_status")
    stage: Mapped["PipelineStage"] = relationship()


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    root_path: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
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
    discovered_at: Mapped[datetime] = mapped_column(server_default=func.now())


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
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DataSubmission(Base):
    __tablename__ = "data_submissions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    submitted_by: Mapped[int] = mapped_column(ForeignKey("operators.id"))
    target_table: Mapped[str] = mapped_column(String(64))
    target_ref_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    validation_status: Mapped[str] = mapped_column(String(16))
    validation_errors: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
