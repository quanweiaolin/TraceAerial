"""
给已入库的老视频补技术元数据/GPS字段，不重新算sha256、不改storage_path/created_at，
只UPDATE缺的列。用于以下场景：
  - videos表里有一批是在支持ffprobe/GPS提取之前扫进去的，字段是空的
  - 以后extract_new_video_fields()逻辑升级了(比如支持了新机型的SRT格式)，
    想对已有数据重新提取一遍

跟scanner.py共用app.services.video_metadata.extract_new_video_fields，
避免"新视频入库"和"老视频回填"两处各写一套提取逻辑。
"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Video
from app.services.video_metadata import extract_new_video_fields


async def backfill_video_metadata(session: AsyncSession, only_missing: bool = True) -> dict:
    """
    only_missing=True(默认): 只处理 duration_seconds IS NULL 的行，
        跑第二次是幂等的(已经补上的行不会重复处理)。
    only_missing=False: 对所有视频强制重新提取一遍(比如提取逻辑升级后想全量刷新)。

    文件在storage_path对应位置已经找不到时跳过并计数，不中断整体回填。
    """
    stmt = select(Video)
    if only_missing:
        stmt = stmt.where(Video.duration_seconds.is_(None))
    result = await session.execute(stmt)
    videos = result.scalars().all()

    checked = updated = missing_file = 0
    for video in videos:
        checked += 1
        path = Path(video.storage_path)
        if not path.is_file():
            missing_file += 1
            continue

        for field, value in extract_new_video_fields(path).items():
            setattr(video, field, value)
        updated += 1

    await session.commit()
    return {"checked": checked, "updated": updated, "missing_file": missing_file}
