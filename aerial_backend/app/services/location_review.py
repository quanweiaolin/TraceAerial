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

from app.config import settings
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
        地点)。**这个分支不会锁定视频**(2026-11-25修——见Finding C2：
        之前这里统一设置video.location_confirmed=True，导致"否定这个
        候选"的决策把视频永久锁死在一个NULL/未定的location_id上，而且
        没有任何解锁入口，同一视频其它待复核item会全部变成永久409。
        只否定了一个候选，不代表地址已经确定，不该上锁)——只标记这条
        item本身为decided，不动video的任何锁定字段。

    已经location_confirmed=true的视频直接
    ValueError("视频地址已锁定，需先解锁")。

    target_location_id必须指向一个真实存在的Location，否则在任何写入
    发生之前就ValueError(f"location_id={target_location_id}不存在")
    (Finding C3a：以前这里直接赋值，等commit时才因为FK约束炸出
    IntegrityError，变成500而不是干净的409)。

    成功后：same_location/new_location两种情况下设置
    video.location_id=target_location_id、video.location_confirmed=
    True、location_confirmed_by/_at(只有这两种情况才真正"锁定"视频，
    见上面C2的说明)；调record_change记录涉及字段的变更(location_id变
    了才记一条，location_confirmed总是要记一条)。

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

    target_location: Location | None = None
    if decision in ("same_location", "new_location"):
        # 存在性校验必须在任何session.add/属性赋值之前做，否则一个不存在
        # 的target_location_id会一路拖到commit()才炸IntegrityError(500)，
        # 而不是这里干净地409(Finding C3a)。
        target_location = await session.get(Location, target_location_id)
        if target_location is None:
            raise ValueError(f"location_id={target_location_id}不存在")

    now = datetime.now(timezone.utc)

    if decision in ("same_location", "new_location"):
        old_location_id = video.location_id
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
    # decision == "different_location"：不碰video的任何字段，只否定了
    # 这一个候选，视频保持未锁定、其它pending item仍然可以正常被决策。

    item.human_decision = decision
    item.decided_by = operator_id
    item.decided_at = now
    item.status = "decided"

    if decision in ("same_location", "new_location") and item.screenshot_path:
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

    if decision in ("same_location", "new_location"):
        if target_location is not None and target_location.representative_video_sha256 is None:
            record_change(
                session,
                entity_type="location",
                entity_id=str(target_location.id),
                field_name="representative_video_sha256",
                old_value=None,
                new_value=sha256_bytes_to_hex(video.sha256),
                changed_by=operator_id,
                reason=reason,
            )
            target_location.representative_video_sha256 = video.sha256

    await session.commit()
    return item


async def unlock_video(
    session: AsyncSession, video_sha256: bytes, operator_id: int, reason: str
) -> None:
    """解锁一条被人工锁定(location_confirmed=true)的视频(Finding C2b)：
    之前`different_location`决策误把视频锁死又没有任何解锁入口，这个
    函数补上那个缺口——把location_confirmed重置为False、清空
    location_confirmed_by/_at，让这条视频重新能进入复核/聚类流程。视频
    本来就没被锁定时直接ValueError，不是"幂等地什么都不做"：解锁一个
    没锁的视频几乎总是调用方逻辑错误，显式报错比静默忽略更安全。"""
    video = await session.get(Video, video_sha256)
    if video is None:
        raise ValueError("视频不存在")
    if not video.location_confirmed:
        raise ValueError("视频地址未锁定，无需解锁")

    record_change(
        session,
        entity_type="video",
        entity_id=sha256_bytes_to_hex(video.sha256),
        field_name="location_confirmed",
        old_value="True",
        new_value="False",
        changed_by=operator_id,
        reason=reason,
    )
    video.location_confirmed = False
    video.location_confirmed_by = None
    video.location_confirmed_at = None

    await session.commit()


async def create_new_location(
    session: AsyncSession, city: str, site_prefix: str, operator_id: int
) -> Location:
    """人工复核时确认"这是个全新地点"，建一条新的Location行(Finding
    C3b)：命名/序号计算跟scanner.py的_assign_city_site新建地址时完全
    同一套逻辑(同city+同site_prefix前缀下递增序号，不同前缀各自独立
    计数)，复用是为了以后两边遇到同一个(city, site_prefix)组合时不会
    算出互相冲突的序号。threshold_meters用跟自动聚类同一个配置项
    (settings.address_cluster_max_diameter_meters)，不是另起一个独立
    的数字——这条新location以后也可能被_assign_city_site的在线聚类拿来
    跟后续视频比较，阈值语义必须一致。"""
    prefix_sites_result = await session.execute(
        select(Location.site)
        .where(Location.city == city)
        .where(Location.site.like(f"{site_prefix}_%"))
    )
    existing_indexes = [
        int(label[len(site_prefix) + 1:])
        for (label,) in prefix_sites_result
        if label[len(site_prefix) + 1:].isdigit()
    ]
    new_site = f"{site_prefix}_{max(existing_indexes, default=0) + 1}"

    new_location = Location(
        city=city,
        site=new_site,
        threshold_meters=settings.address_cluster_max_diameter_meters,
    )
    session.add(new_location)
    await session.flush()  # 拿到new_location.id

    record_change(
        session,
        entity_type="location",
        entity_id=str(new_location.id),
        field_name="created",
        old_value=None,
        new_value=f"{city}/{new_site}",
        changed_by=operator_id,
    )

    await session.commit()
    return new_location


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
