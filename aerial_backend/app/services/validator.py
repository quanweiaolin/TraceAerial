"""
学生提交数据的校验服务。

约定：所有"学生可写"的接口(比如标注结果入库、场景事件入库)在真正写入
业务表之前，都先过一遍这里的validate_*函数；无论通过与否都写一行
data_submissions做审计；连续失败达到阈值，升级提醒负责人。

具体校验规则(字段是否齐全/JSON schema是否合法/文件是否真实存在于对象存储等)
按各业务表分别实现，这里给出annotation批次校验的示例。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DataSubmission, Operator
from app.services.notifier import enqueue_validation_escalation


class ValidationResult:
    def __init__(self, ok: bool, errors: dict | None = None):
        self.ok = ok
        self.errors = errors or {}


def validate_annotation_payload(payload: dict) -> ValidationResult:
    errors = {}
    required = ["video_sha256", "annotation_type", "storage_path", "format"]
    for field in required:
        if not payload.get(field):
            errors[field] = "字段缺失"

    if payload.get("frame_start") is not None and payload.get("frame_end") is not None:
        if payload["frame_start"] > payload["frame_end"]:
            errors["frame_range"] = "frame_start不能大于frame_end"

    # 实际项目里这里应该再检查 storage_path 对应的文件是否真的存在于对象存储，
    # 以及JSON内容本身是否符合COCO/自定义schema，示例先省略I/O部分
    return ValidationResult(ok=not errors, errors=errors)


async def record_submission_and_maybe_escalate(
    session: AsyncSession,
    *,
    operator: Operator,
    target_table: str,
    target_ref_id: int | None,
    payload: dict,
    result: ValidationResult,
) -> DataSubmission:
    submission = DataSubmission(
        submitted_by=operator.id,
        target_table=target_table,
        target_ref_id=target_ref_id,
        payload=payload,
        validation_status="passed" if result.ok else "failed",
        validation_errors=result.errors or None,
    )
    session.add(submission)
    await session.flush()

    if not result.ok:
        recent_fail_count = await _count_recent_consecutive_failures(session, operator.id)
        if recent_fail_count >= settings.validation_fail_escalation_threshold:
            await enqueue_validation_escalation(session, operator.username, recent_fail_count)

    await session.commit()
    return submission


async def _count_recent_consecutive_failures(session: AsyncSession, operator_id: int) -> int:
    """取该人最近N条提交，数一数从最新开始连续失败了几条(中间只要有一条passed就断掉)。"""
    result = await session.execute(
        select(DataSubmission.validation_status)
        .where(DataSubmission.submitted_by == operator_id)
        .order_by(DataSubmission.created_at.desc())
        .limit(20)
    )
    count = 0
    for (status,) in result:
        if status == "failed":
            count += 1
        else:
            break
    return count
