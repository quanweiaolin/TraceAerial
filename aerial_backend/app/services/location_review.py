"""
地址复核队列核心逻辑：把Task1的ORM模型、Task3的视觉比对、Task4的变更
审计串起来，提供三个动作：

  1. create_review_item —— 给一条"待复核视频 vs 一个候选地址"建一条
     复核item，顺带跑视觉比对打个参考分。
  2. apply_human_decision —— 人工在复核队列里对某条item做决策，落地到
     videos表(location_id/location_confirmed)+反向回填候选地址的参考
     截图+记变更历史。
  3. merge_location_groups —— 人工确认多个locations其实是同一个物理
     地址，合并成一个LocationGroup。

跟location_history.py同样的事务边界约定：这里的函数自己commit(没有
上层router在这个任务范围内接住)，内部record_change只是session.add，
真正落盘发生在各函数末尾的await session.commit()。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Location, LocationGroup, LocationReviewItem, Video
from app.services.hashing import sha256_bytes_to_hex
from app.services.location_history import record_change
from app.services.location_visual_match import compare_frames


async def create_review_item(
    session: AsyncSession,
    video_sha256: bytes,
    candidate_location_id: int | None,
    video_frame_path: str,
    candidate_frame_path: str | None,
) -> LocationReviewItem:
    """建一条复核item。`video_frame_path`是调用方已经提取好的这条视频的
    截图本地路径(不是原始视频路径)——同一条视频要跟多个候选location分别
    比对时，调用方只应该提取一次截图，这里不做任何截图提取。

    `video_frame_path`这个路径本身读不到(文件不存在)才是
    status="screenshot_failed"；compare_frames内部对"无法估计"这类情况
    已经自己返回描述性失败结果(matched=False)而不是抛异常，那种情况仍然
    正常建item、status="pending"。
    """
    if not Path(video_frame_path).is_file():
        item = LocationReviewItem(
            video_sha256=video_sha256,
            candidate_location_id=candidate_location_id,
            screenshot_path=None,
            visual_match_result=None,
            status="screenshot_failed",
        )
        session.add(item)
        await session.commit()
        return item

    visual_match_result = None
    if candidate_frame_path is not None:
        visual_match_result = compare_frames(video_frame_path, candidate_frame_path)

    item = LocationReviewItem(
        video_sha256=video_sha256,
        candidate_location_id=candidate_location_id,
        screenshot_path=video_frame_path,
        visual_match_result=visual_match_result,
        status="pending",
    )
    session.add(item)
    await session.commit()
    return item


async def apply_human_decision(
    session: AsyncSession,
    item_id: int,
    decision: str,
    operator_id: int,
    *,
    target_location_id: int | None = None,
    reason: str | None = None,
) -> LocationReviewItem:
    """人工对一条复核item做决策，三种`decision`取值精确语义(2026-11-25
    预检钉死，不要自己猜)：

      - "same_location"/"new_location"：target_location_id必填，否则
        ValueError("缺少target_location_id")。这两种取值在代码里走
        同一分支——唯一区别只是"这个地点是不是复核候选本身"，对数据库
        写入没有区别。
      - "different_location"：target_location_id必须是None，否则
        ValueError("different_location不应该带target_location_id")；
        video.location_id保持原值不变(可能还是NULL，也可能是别的待定
        值，这个决策只是否定了"是这个候选"，不代表已经确定是别的某个
        地点)。

    已经location_confirmed=true的视频直接
    ValueError("视频地址已锁定，需先解锁")。

    成功后：same_location/new_location两种情况下设置
    video.location_id=target_location_id；三种情况下都设置
    video.location_confirmed=True、location_confirmed_by/_at；调
    record_change记录涉及字段的变更(location_id变了才记一条，
    location_confirmed总是要记一条)。

    反向回填截图：decision是same_location或new_location且
    target_location_id对应的Location.representative_frame_path当前
    为空时，用这个item的screenshot_path去回填，同样调record_change记
    一条(entity_type="location")。screenshot_path本身为空时跳过回填，
    不报错。
    """
    item = await session.get(LocationReviewItem, item_id)
    if item is None:
        raise ValueError("复核item不存在")

    if decision in ("same_location", "new_location"):
        if target_location_id is None:
            raise ValueError("缺少target_location_id")
    elif decision == "different_location":
        if target_location_id is not None:
            raise ValueError("different_location不应该带target_location_id")
    else:
        raise ValueError(f"未知的decision取值: {decision}")

    video = await session.get(Video, item.video_sha256)
    if video is None:
        raise ValueError("视频不存在")
    if video.location_confirmed:
        raise ValueError("视频地址已锁定，需先解锁")

    now = datetime.now(timezone.utc)

    old_location_id = video.location_id
    if decision in ("same_location", "new_location"):
        if old_location_id != target_location_id:
            record_change(
                session,
                entity_type="video",
                entity_id=sha256_bytes_to_hex(video.sha256),
                field_name="location_id",
                old_value=str(old_location_id) if old_location_id is not None else None,
                new_value=str(target_location_id),
                changed_by=operator_id,
                reason=reason,
            )
            video.location_id = target_location_id

    record_change(
        session,
        entity_type="video",
        entity_id=sha256_bytes_to_hex(video.sha256),
        field_name="location_confirmed",
        old_value=str(video.location_confirmed),
        new_value=str(True),
        changed_by=operator_id,
        reason=reason,
    )
    video.location_confirmed = True
    video.location_confirmed_by = operator_id
    video.location_confirmed_at = now

    item.human_decision = decision
    item.decided_by = operator_id
    item.decided_at = now
    item.status = "decided"

    if decision in ("same_location", "new_location") and item.screenshot_path:
        target_location = await session.get(Location, target_location_id)
        if target_location is not None and target_location.representative_frame_path is None:
            record_change(
                session,
                entity_type="location",
                entity_id=str(target_location.id),
                field_name="representative_frame_path",
                old_value=None,
                new_value=item.screenshot_path,
                changed_by=operator_id,
                reason=reason,
            )
            target_location.representative_frame_path = item.screenshot_path

    await session.commit()
    return item


async def merge_location_groups(
    session: AsyncSession,
    location_ids: list[int],
    group_name: str,
    operator_id: int,
) -> LocationGroup:
    """人工确认多个locations是同一个物理地址，合并成一个LocationGroup。

    任一location_id已属于别的location_group_id则
    ValueError("已属于另一个分组")；否则新建LocationGroup、把这些
    locations.location_group_id指过去、record_change记录。
    """
    result = await session.execute(select(Location).where(Location.id.in_(location_ids)))
    locations = result.scalars().all()

    found_ids = {loc.id for loc in locations}
    missing_ids = set(location_ids) - found_ids
    if missing_ids:
        raise ValueError(f"location不存在: {sorted(missing_ids)}")

    for loc in locations:
        if loc.location_group_id is not None:
            raise ValueError("已属于另一个分组")

    group = LocationGroup(
        name=group_name,
        confirmed_by=operator_id,
        confirmed_at=datetime.now(timezone.utc),
    )
    session.add(group)
    await session.flush()

    for loc in locations:
        record_change(
            session,
            entity_type="location",
            entity_id=str(loc.id),
            field_name="location_group_id",
            old_value=None,
            new_value=str(group.id),
            changed_by=operator_id,
        )
        loc.location_group_id = group.id

    await session.commit()
    return group
