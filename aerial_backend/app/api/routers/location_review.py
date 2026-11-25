"""
地址人工复核队列的HTTP入口：把Task2的鉴权、Task3的截图提取/视觉比对、
Task5的复核队列核心逻辑、Task6的LLM影子判断串起来。

所有接口统一用`Depends(require_location_reviewer)`鉴权(见
app.services.location_auth)，不区分具体业务权限粒度——能调用这些接口的
operator默认就有权限做队列里的所有操作。

`POST /location-review/queue/generate`是整个复核流程的入口：在这之前
没有任何代码会往location_review_items表里写数据，这个接口负责"给一个
视频找出同城市的候选地址集合，对每个候选建一条复核item，并顺带跑一次
LLM影子判断"。其它几个接口(列表/详情/决策/合并/一致率)都是对已经建好的
数据做读取或状态流转，直接转调Task5/Task6写好的函数，这里只做HTTP层的
请求体解析+异常转换。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Location, LocationReviewItem, Operator, Video
from app.services import location_llm_judge, location_review
from app.services.hashing import sha256_hex_to_bytes
from app.services.location_auth import require_location_reviewer
from app.services.location_visual_match import extract_frame

router = APIRouter(prefix="/location-review", tags=["location_review"])


@router.get("/queue")
async def list_queue(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """列出复核队列，`status`不传时返回全部，传了就按状态过滤
    (status对应LocationReviewItem.status，比如"pending")。"""
    stmt = select(LocationReviewItem).order_by(LocationReviewItem.id.desc())
    if status is not None:
        stmt = stmt.where(LocationReviewItem.status == status)
    items = (await session.execute(stmt)).scalars().all()
    return items


@router.get("/queue/{item_id}")
async def get_queue_item(
    item_id: int,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    item = await session.get(LocationReviewItem, item_id)
    if item is None:
        raise HTTPException(404, "复核item不存在")
    return item


class GenerateQueueIn(BaseModel):
    video_sha256: str
    video_path: str


@router.post("/queue/generate")
async def generate_queue(
    body: GenerateQueueIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """给一条视频生成复核候选：按video.city找同城市、已有代表截图的全部
    locations作为候选集，对每个候选建一条复核item；候选集为空时建一条
    candidate_location_id=None的"新地点"item。

    截图只提取一次(不是每个候选都重新提取)，提取失败不让整个请求500，
    而是把extracted_frame_path当成空字符串继续走下面的流程(让
    create_review_item自己判定成status="screenshot_failed")。

    每条成功建出来、真的有两张图可比的item(status != "screenshot_failed"
    且candidate_frame_path不为空)，额外跑一次LLM影子判断；只有
    same_location不是None(判断真的成功)时才写入llm_verdict，判断失败
    时保持llm_verdict为NULL，不跟真实的"不同意"混在一起。
    """
    video_sha256 = sha256_hex_to_bytes(body.video_sha256)
    video = await session.get(Video, video_sha256)
    if video is None:
        raise HTTPException(404, "视频不存在")
    if not video.city:
        raise HTTPException(422, "该视频没有city信息，无法生成候选")

    candidates_result = await session.execute(
        select(Location).where(
            Location.city == video.city,
            Location.representative_frame_path.isnot(None),
        )
    )
    candidates = candidates_result.scalars().all()

    fd, extracted_frame_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        extract_frame(body.video_path, extracted_frame_path)
    except ValueError:
        extracted_frame_path = ""

    created_items: list[LocationReviewItem] = []
    if candidates:
        for candidate in candidates:
            item = await location_review.create_review_item(
                session,
                video_sha256,
                candidate.id,
                extracted_frame_path,
                candidate.representative_frame_path,
            )
            created_items.append(item)

            if item.status != "screenshot_failed" and candidate.representative_frame_path:
                try:
                    verdict = await location_llm_judge.judge_async(
                        item, extracted_frame_path, candidate.representative_frame_path
                    )
                except Exception:
                    verdict = None

                if verdict is not None and verdict.get("same_location") is not None:
                    item.llm_verdict = verdict
                    item.llm_verdict_at = datetime.now(timezone.utc)
                    await session.commit()
    else:
        item = await location_review.create_review_item(
            session, video_sha256, None, extracted_frame_path, None
        )
        created_items.append(item)

    return created_items


class DecideIn(BaseModel):
    decision: str
    target_location_id: int | None = None
    reason: str | None = None


@router.post("/queue/{item_id}/decide")
async def decide_queue_item(
    item_id: int,
    body: DecideIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    try:
        item = await location_review.apply_human_decision(
            session,
            item_id,
            body.decision,
            operator.id,
            target_location_id=body.target_location_id,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return item


class MergeGroupsIn(BaseModel):
    location_ids: list[int]
    group_name: str


@router.post("/groups/merge")
async def merge_groups(
    body: MergeGroupsIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    try:
        group = await location_review.merge_location_groups(
            session, body.location_ids, body.group_name, operator.id
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return group


@router.get("/agreement-rate")
async def agreement_rate(
    exclude_corrected: bool = True,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    return await location_llm_judge.compute_agreement_rate(
        session, exclude_corrected=exclude_corrected
    )
