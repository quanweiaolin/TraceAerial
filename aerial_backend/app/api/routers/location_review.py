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

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Location, LocationGroup, LocationReviewItem, Operator, Video
from app.services import location_llm_judge, location_review
from app.services.geo import distance_3d_meters
from app.services.hashing import sha256_bytes_to_hex, sha256_hex_to_bytes
from app.services.location_auth import require_location_reviewer
from app.services.location_visual_match import extract_frame

router = APIRouter(prefix="/location-review", tags=["location_review"])


def _resolve_height(video: Video) -> float | None:
    """每条视频各自独立判断：`srt_height`不为空用`srt_height`，否则退回
    `flight_height`；两者都为空时返回`None`(由调用方决定GPS距离算不出来
    的后续处理，不在这里报错)。"""
    return video.srt_height if video.srt_height is not None else video.flight_height


async def _serialize_review_item(session: AsyncSession, item: LocationReviewItem) -> dict:
    """手建dict返回，不直接把ORM对象交给FastAPI——`video_sha256`是bytea，
    `jsonable_encoder`会尝试把它当UTF-8字符串`.decode()`，对真实sha256
    摘要几乎总是报`UnicodeDecodeError`(见app/api/routers/videos.py同样的
    手建dict+hex编码惯例)。

    额外给前端拼上GPS距离/候选地点身份/视频识别信息，省得前端自己再发
    请求查这些(见Task2 brief)。"""
    video = await session.get(Video, item.video_sha256)
    video_filename = Path(video.storage_path).name if video is not None else None

    candidate: dict | None = None
    candidate_location: Location | None = None
    if item.candidate_location_id is not None:
        candidate_location = await session.get(Location, item.candidate_location_id)
        if candidate_location is not None:
            group_name = None
            if candidate_location.location_group_id is not None:
                group = await session.get(LocationGroup, candidate_location.location_group_id)
                group_name = group.name if group is not None else None
            candidate = {
                "id": candidate_location.id,
                "city": candidate_location.city,
                "site": candidate_location.site,
                "representative_frame_path": candidate_location.representative_frame_path,
                "group_name": group_name,
            }

    gps_distance_meters: float | None = None
    if (
        video is not None
        and candidate_location is not None
        and candidate_location.representative_video_sha256 is not None
    ):
        ref_video = await session.get(Video, candidate_location.representative_video_sha256)
        if ref_video is not None:
            if (
                video.collect_lon is not None
                and video.collect_lat is not None
                and ref_video.collect_lon is not None
                and ref_video.collect_lat is not None
            ):
                video_height = _resolve_height(video)
                ref_height = _resolve_height(ref_video)
                if video_height is not None and ref_height is not None:
                    gps_distance_meters = distance_3d_meters(
                        video.collect_lon, video.collect_lat, video_height,
                        ref_video.collect_lon, ref_video.collect_lat, ref_height,
                    )

    return {
        "id": item.id,
        "video_sha256": sha256_bytes_to_hex(item.video_sha256),
        "candidate_location_id": item.candidate_location_id,
        "screenshot_path": item.screenshot_path,
        "screenshot_paths": [item.screenshot_path] if item.screenshot_path is not None else [],
        "visual_match_result": item.visual_match_result,
        "llm_verdict": item.llm_verdict,
        "llm_verdict_at": item.llm_verdict_at,
        "human_decision": item.human_decision,
        "decided_by": item.decided_by,
        "decided_at": item.decided_at,
        "status": item.status,
        "created_at": item.created_at,
        "candidate": candidate,
        "gps_distance_meters": gps_distance_meters,
        "video_filename": video_filename,
        "video_city": video.city if video is not None else None,
        "video_collected_at": video.collected_at if video is not None else None,
        "video_location_confirmed": video.location_confirmed if video is not None else False,
    }


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
    return [await _serialize_review_item(session, item) for item in items]


@router.get("/queue/{item_id}")
async def get_queue_item(
    item_id: int,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    item = await session.get(LocationReviewItem, item_id)
    if item is None:
        raise HTTPException(404, "复核item不存在")
    return await _serialize_review_item(session, item)


class GenerateQueueIn(BaseModel):
    video_sha256: str
    video_path: str


async def _find_pending_item(
    session: AsyncSession, video_sha256: bytes, candidate_location_id: int | None
) -> LocationReviewItem | None:
    """同一个(video_sha256, candidate_location_id)组合已经有一条pending的
    复核item时返回它，供generate_queue跳过重复创建(见Finding I3)。"""
    stmt = select(LocationReviewItem).where(
        LocationReviewItem.video_sha256 == video_sha256,
        LocationReviewItem.candidate_location_id.is_(None)
        if candidate_location_id is None
        else LocationReviewItem.candidate_location_id == candidate_location_id,
        LocationReviewItem.status == "pending",
    )
    return (await session.execute(stmt)).scalars().first()


@router.post("/queue/generate")
async def generate_queue(
    body: GenerateQueueIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """给一条视频生成复核候选：按video.city找同城市、已有代表截图的全部
    locations作为候选集，对每个候选建一条复核item；候选集为空时建一条
    candidate_location_id=None的"新地点"item。

    已经location_confirmed=true的视频直接拒绝——不需要(也不应该)再生成
    候选(Finding I3a)。同一个(video, candidate)组合已有pending item时
    跳过重建，直接复用已有的那条，返回的列表里包含它(Finding I3b)。

    截图提取(ffmpeg子进程)丢进线程池跑，不阻塞事件循环(Finding I2，跟
    scanner.py里extract_new_video_fields/compute_sha256_and_sha1同样的
    asyncio.to_thread惯例)。提取失败不让整个请求500，而是把
    extracted_frame_path当成空字符串继续走下面的流程(让
    create_review_item自己判定成status="screenshot_failed")。截图落在
    settings.location_review_frames_dir下的持久目录，不是/tmp(Finding
    C4——/tmp上的文件可能被系统清理，但这条路径要长期存进
    screenshot_path/representative_frame_path)。

    每条成功建出来、真的有两张图可比的item(status != "screenshot_failed"
    且candidate_frame_path不为空)，额外跑一次LLM影子判断；只有
    same_location不是None(判断真的成功)时才写入llm_verdict，判断失败
    时保持llm_verdict为NULL，不跟真实的"不同意"混在一起。
    """
    video_sha256 = sha256_hex_to_bytes(body.video_sha256)
    video = await session.get(Video, video_sha256)
    if video is None:
        raise HTTPException(404, "视频不存在")
    if video.location_confirmed:
        raise HTTPException(409, "该视频地址已锁定，无需重新生成候选")
    if not video.city:
        raise HTTPException(422, "该视频没有city信息，无法生成候选")

    candidates_result = await session.execute(
        select(Location).where(
            Location.city == video.city,
            Location.representative_frame_path.isnot(None),
        )
    )
    candidates = candidates_result.scalars().all()

    frames_dir = Path(settings.location_review_frames_dir or "location_review_frames")
    frames_dir.mkdir(parents=True, exist_ok=True)
    # 文件名用这次请求的视频sha256 hex，同一视频重复生成候选时会被覆盖写
    # 同一张图(内容本身就是这条视频的截图，覆盖不丢信息)，不同视频之间
    # 不会冲突——不用tempfile.mkstemp(那会落在/tmp，见Finding C4)。
    extracted_frame_path = str(frames_dir / f"{body.video_sha256}_video.jpg")
    try:
        await asyncio.to_thread(extract_frame, body.video_path, extracted_frame_path)
    except ValueError:
        extracted_frame_path = ""

    created_items: list[LocationReviewItem] = []
    if candidates:
        for candidate in candidates:
            existing = await _find_pending_item(session, video_sha256, candidate.id)
            if existing is not None:
                created_items.append(existing)
                continue

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
        existing = await _find_pending_item(session, video_sha256, None)
        if existing is not None:
            created_items.append(existing)
        else:
            item = await location_review.create_review_item(
                session, video_sha256, None, extracted_frame_path, None
            )
            created_items.append(item)

    return [await _serialize_review_item(session, item) for item in created_items]


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
    return await _serialize_review_item(session, item)


class UnlockVideoIn(BaseModel):
    reason: str


@router.post("/videos/{video_sha256_hex}/unlock")
async def unlock_video(
    video_sha256_hex: str,
    body: UnlockVideoIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """解锁一条已被人工锁定(location_confirmed=true)的视频——`different_
    location`决策不再自动锁定视频(见apply_human_decision)，但一条视频
    一旦被`same_location`/`new_location`锁定错了，仍然需要这个显式解锁
    入口才能重新进入复核流程(Finding C2c)。"""
    try:
        await location_review.unlock_video(
            session, sha256_hex_to_bytes(video_sha256_hex), operator.id, body.reason
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


class CreateLocationIn(BaseModel):
    city: str
    site_prefix: str


@router.post("/locations")
async def create_location(
    body: CreateLocationIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """人工复核时发现是个全新地点，没有任何现有location可以归——建一条
    新的Location行，返回它的id/city/site，供调用方接着拿这个id去调
    `/queue/{item_id}/decide`当`target_location_id`(Finding C3b)。"""
    new_location = await location_review.create_new_location(
        session, body.city, body.site_prefix, operator.id
    )
    return {"id": new_location.id, "city": new_location.city, "site": new_location.site}


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


@router.get("/frame")
async def get_frame(
    path: str,
    operator: Operator = Depends(require_location_reviewer),
):
    """把截图文件的字节传给前端——screenshot_paths/representative_frame_path
    存的是服务器本地文件系统路径，浏览器没法直接访问，也没法在<img src>上带
    Authorization头，所以前端要先authed fetch这个接口拿字节，再用
    URL.createObjectURL展示(见location_review_web/src/api/client.ts的
    apiFetchBlob)。只允许访问location_review_frames_dir目录下的文件，
    防止用这个接口当成任意文件读取的后门。"""
    frames_dir = Path(settings.location_review_frames_dir or "location_review_frames").resolve()
    requested = Path(path).resolve()
    if not requested.is_relative_to(frames_dir):
        raise HTTPException(404, "文件不存在")
    if not requested.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(requested)


@router.get("/locations")
async def list_locations(
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """全量返回locations表(不分页——当前量级42条，忽略分页)，供前端做
    地址候选下拉/人工复核时的参照列表。"""
    locations = (await session.execute(select(Location))).scalars().all()
    return [
        {
            "id": location.id,
            "city": location.city,
            "site": location.site,
            "representative_frame_path": location.representative_frame_path,
            "location_group_id": location.location_group_id,
        }
        for location in locations
    ]


@router.get("/groups")
async def list_groups(
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """列出全部LocationGroup，每个group内联查一下它的members(location_
    group_id==group.id的Location集合)——group数量就个位数/十位数级别，
    这里的N+1不值得优化成一次join。"""
    groups = (await session.execute(select(LocationGroup))).scalars().all()
    result = []
    for group in groups:
        members_result = await session.execute(
            select(Location).where(Location.location_group_id == group.id)
        )
        members = members_result.scalars().all()
        result.append(
            {
                "id": group.id,
                "name": group.name,
                "confirmed_at": group.confirmed_at,
                "members": [
                    {"id": member.id, "city": member.city, "site": member.site}
                    for member in members
                ],
            }
        )
    return result


@router.get("/folder-audit")
async def folder_audit(
    storage_path_prefix: str,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """按`storage_path`前缀批量审查一批视频的落库情况，供人工核对某个
    采集文件夹整体对应到的地址/锁定状态。`.like()`用参数绑定而不是
    自己拼SQL字符串——`storage_path_prefix`是未受信任的查询参数。

    加`.limit(500)`防止一个过于宽泛(比如空字符串/过短)的前缀匹配出整张
    videos表、拖垮数据库——前端据此判断"结果可能被截断"，提示用户输入
    更精确的前缀，而不是后端再加一个专门的截断标记字段(Finding I5)。"""
    videos_result = await session.execute(
        select(Video).where(Video.storage_path.like(f"{storage_path_prefix}%")).limit(500)
    )
    videos = videos_result.scalars().all()
    return [
        {
            "video_sha256": sha256_bytes_to_hex(video.sha256),
            "storage_path": video.storage_path,
            "location_id": video.location_id,
            "is_stable": video.is_stable,
            "collect_lon": video.collect_lon,
            "collect_lat": video.collect_lat,
        }
        for video in videos
    ]
