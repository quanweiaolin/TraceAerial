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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import (
    Location,
    LocationGroup,
    LocationNamingAlert,
    LocationReviewItem,
    Operator,
    Video,
)
from app.services import location_llm_judge, location_review
from app.services.geo import distance_3d_meters
from app.services.hashing import sha256_bytes_to_hex, sha256_hex_to_bytes
from app.services.location_auth import require_location_reviewer
from app.services.location_history import record_change
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
                "aliases": group.aliases,
            }
        )
    return result


class UpdateGroupAliasIn(BaseModel):
    key: str
    value: str | None = None


@router.patch("/groups/{group_id}/aliases")
async def update_group_alias(
    group_id: int,
    body: UpdateGroupAliasIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """新增/修改/删除一个客户别名(`value`为`None`代表删除)。整字典替换
    赋值(`group.aliases = new_aliases`)而不是原地`.pop()`/`[key]=...`——
    SQLAlchemy对JSONB列的变更追踪靠赋值触发脏标记，原地改字典ORM感知
    不到，不会真的写进数据库。"""
    group = await session.get(LocationGroup, group_id)
    if group is None:
        raise HTTPException(404, "分组不存在")
    old_value = group.aliases.get(body.key)
    new_aliases = dict(group.aliases)
    if body.value is None:
        new_aliases.pop(body.key, None)
    else:
        new_aliases[body.key] = body.value
    record_change(
        session, entity_type="location_group", entity_id=str(group_id),
        field_name=f"alias:{body.key}", old_value=old_value, new_value=body.value,
        changed_by=operator.id,
    )
    group.aliases = new_aliases
    await session.commit()
    return {"id": group.id, "aliases": group.aliases}


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


def _serialize_naming_alert(alert: LocationNamingAlert, locations_by_id: dict[int, Location]) -> dict:
    """附上每个location_id当前的city/site，方便前端直接展示而不用再单独
    查一次(Task3 brief要求批量补，见list_naming_alerts里一次性查完整批
    Location再传进来，不在这里per-alert查询)。"""
    return {
        "id": alert.id,
        "alert_type": alert.alert_type,
        "city": alert.city,
        "location_ids": alert.location_ids,
        "raw_site_votes": alert.raw_site_votes,
        "suggested_value": alert.suggested_value,
        "status": alert.status,
        "resolved_by": alert.resolved_by,
        "resolved_at": alert.resolved_at,
        "created_at": alert.created_at,
        "locations": [
            {
                "id": location_id,
                "city": locations_by_id[location_id].city,
                "site": locations_by_id[location_id].site,
            }
            for location_id in alert.location_ids
            if location_id in locations_by_id
        ],
    }


@router.get("/naming-alerts")
async def list_naming_alerts(
    status: Literal["pending", "confirmed", "dismissed"] = "pending",
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """列出命名审核告警(见app.services.location_naming_audit)，默认只看
    pending的。为每个alert涉及的location_id附上当前的city/site——批量
    查一次这批alert覆盖到的全部location_id，不对每条alert单独查
    (避免N+1)。"""
    alerts_result = await session.execute(
        select(LocationNamingAlert)
        .where(LocationNamingAlert.status == status)
        .order_by(LocationNamingAlert.id.desc())
    )
    alerts = alerts_result.scalars().all()

    all_location_ids: set[int] = set()
    for alert in alerts:
        all_location_ids.update(alert.location_ids)

    locations_by_id: dict[int, Location] = {}
    if all_location_ids:
        locations_result = await session.execute(
            select(Location).where(Location.id.in_(all_location_ids))
        )
        locations_by_id = {location.id: location for location in locations_result.scalars().all()}

    return [_serialize_naming_alert(alert, locations_by_id) for alert in alerts]


class ConfirmNamingAlertIn(BaseModel):
    chosen_value: str | None = None


async def _get_pending_naming_alert(session: AsyncSession, alert_id: int) -> LocationNamingAlert:
    """404/409的公共校验逻辑：alert不存在报404，存在但已经不是pending(已经
    被confirm或dismiss过)报409——不允许重复处理同一条alert(brief要求)。"""
    alert = await session.get(LocationNamingAlert, alert_id)
    if alert is None:
        raise HTTPException(404, "提示不存在")
    if alert.status != "pending":
        raise HTTPException(409, "该提示已处理，不能重复操作")
    return alert


async def _serialize_naming_alert_with_locations(
    session: AsyncSession, alert: LocationNamingAlert
) -> dict:
    """confirm/dismiss返回单条alert时复用跟列表接口一样的序列化形状——
    重新查一次这条alert涉及的location(confirm的name_drift分支可能刚改过
    其中一个的site，这里查到的是改写之后的最新值)。"""
    locations_by_id: dict[int, Location] = {}
    if alert.location_ids:
        locations_result = await session.execute(
            select(Location).where(Location.id.in_(alert.location_ids))
        )
        locations_by_id = {location.id: location for location in locations_result.scalars().all()}
    return _serialize_naming_alert(alert, locations_by_id)


@router.post("/naming-alerts/{alert_id}/confirm")
async def confirm_naming_alert(
    alert_id: int,
    body: ConfirmNamingAlertIn,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """确认一条命名审核告警。

    `alert_type=="name_drift"`：`chosen_value`必须非空(trim后)，把alert
    唯一涉及的那个location(`location_ids[0]`，name_drift从构造时起就只有
    一个location_id——见location_naming_audit.build_alert_candidates)的
    `site`改写成`chosen_value`。这会跟其它location撞`UNIQUE(city, site)`
    时，捕获写入阶段的`IntegrityError`，返回400而不是让它变成500。

    `alert_type=="merge_candidate"`：这个endpoint不碰任何Location行——
    真正的合并走现有的`/location-review/groups/merge`，这里只是把alert
    本身标记为已确认，表示"人工已经看过这组投票分布并同意需要合并"。

    两种情况成功后都设置`status="confirmed"`、`resolved_by`/`resolved_at`
    并commit。"""
    alert = await _get_pending_naming_alert(session, alert_id)

    if alert.alert_type == "name_drift":
        chosen_value = (body.chosen_value or "").strip()
        if not chosen_value:
            raise HTTPException(400, "chosen_value不能为空")

        location_id = alert.location_ids[0]
        location = await session.get(Location, location_id)
        if location is None:
            raise HTTPException(404, "该提示涉及的地址不存在")

        old_site = location.site
        location.site = chosen_value
        record_change(
            session, entity_type="location", entity_id=str(location_id),
            field_name="site", old_value=old_site, new_value=chosen_value,
            changed_by=operator.id,
        )

        alert.status = "confirmed"
        alert.resolved_by = operator.id
        alert.resolved_at = datetime.now(timezone.utc)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(400, f"同城市下已经存在site=\"{chosen_value}\"的地址，不能重名") from None
    else:
        alert.status = "confirmed"
        alert.resolved_by = operator.id
        alert.resolved_at = datetime.now(timezone.utc)
        await session.commit()

    return await _serialize_naming_alert_with_locations(session, alert)


@router.post("/naming-alerts/{alert_id}/dismiss")
async def dismiss_naming_alert(
    alert_id: int,
    session: AsyncSession = Depends(get_session),
    operator: Operator = Depends(require_location_reviewer),
):
    """驳回一条命名审核告警：不碰任何Location行，只把alert本身标记为
    dismissed(表示人工已经看过，认为不需要处理)。"""
    alert = await _get_pending_naming_alert(session, alert_id)
    alert.status = "dismissed"
    alert.resolved_by = operator.id
    alert.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    return await _serialize_naming_alert_with_locations(session, alert)
