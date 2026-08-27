"""
视频相关接口示例。实际项目里 annotation/inference/scene_events 等
每类资产各开一个router，写法和 submit_annotation 这个例子保持一致：
   校验 -> 记审计 -> (通过就)写业务表 -> 更新pipeline状态 -> 提交事务
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Operator, Video, VideoPipelineStatus
from app.services.hashing import sha256_bytes_to_hex, sha256_hex_to_bytes
from app.services.validator import (
    ValidationResult,
    record_submission_and_maybe_escalate,
    validate_annotation_payload,
)

router = APIRouter(prefix="/videos", tags=["videos"])


@router.get("/{sha256_hex}/progress")
async def get_video_progress(sha256_hex: str, session: AsyncSession = Depends(get_session)):
    """直接用之前SQL里定义好的 v_video_progress 视图，一次拿到该视频所有阶段状态。"""
    result = await session.execute(
        text("SELECT * FROM v_video_progress WHERE sha256 = :sha"),
        {"sha": sha256_hex_to_bytes(sha256_hex)},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(404, "video not found")
    return dict(row)


@router.get("")
async def list_videos(
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """按需展示资产：这类"直接查询"接口就是普通SQLAlchemy查询，前端传筛选条件即可扩展。"""
    result = await session.execute(
        select(Video)
        .where(Video.deleted_at.is_(None))
        .order_by(Video.collected_at.desc())
        .limit(limit)
        .offset(offset)
    )
    videos = result.scalars().all()
    return [
        {
            "sha256": sha256_bytes_to_hex(v.sha256),
            "original_name": v.original_name,
            "storage_path": v.storage_path,
            "collected_at": v.collected_at,
        }
        for v in videos
    ]


class AnnotationSubmitIn(BaseModel):
    video_sha256: str
    annotation_type: str
    storage_path: str
    format: str
    frame_start: int | None = None
    frame_end: int | None = None
    object_count: int | None = None


@router.post("/annotations")
async def submit_annotation(
    body: AnnotationSubmitIn,
    operator_id: int,  # 实际项目里从登录态/JWT里取，这里简化成参数
    session: AsyncSession = Depends(get_session),
):
    """学生提交标注结果的入口：先校验，校验失败记审计+可能升级提醒，不写业务表。"""
    operator = await session.get(Operator, operator_id)
    if operator is None:
        raise HTTPException(404, "operator not found")

    payload = body.model_dump()
    result: ValidationResult = validate_annotation_payload(payload)

    submission = await record_submission_and_maybe_escalate(
        session,
        operator=operator,
        target_table="annotation_batches",
        target_ref_id=None,
        payload=payload,
        result=result,
    )

    if not result.ok:
        raise HTTPException(422, detail={"errors": result.errors, "submission_id": submission.id})

    # 校验通过，真正写入业务表(此处省略AnnotationBatch的ORM定义，按models.py同样模式加)
    # 并显式更新pipeline状态，替代了之前的DB触发器方案：
    stage_row = await session.execute(
        text("SELECT id FROM pipeline_stages WHERE code = 'annotation'")
    )
    stage_id = stage_row.scalar_one()
    await session.execute(
        text(
            """
            UPDATE video_pipeline_status
            SET status = 'in_progress', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE video_sha256 = :video AND stage_id = :stage
            """
        ),
        {"video": sha256_hex_to_bytes(body.video_sha256), "stage": stage_id},
    )
    await session.commit()
    return {"ok": True, "submission_id": submission.id}
