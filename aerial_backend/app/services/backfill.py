"""
给已入库的老视频补技术元数据/GPS字段，不重新算sha256、不改storage_path/created_at，
只UPDATE缺的列。用于以下场景：
  - videos表里有一批是在支持ffprobe/GPS提取之前扫进去的，字段是空的
  - 以后extract_new_video_fields()逻辑升级了(比如支持了新机型的SRT格式)，
    想对已有数据重新提取一遍

跟scanner.py共用app.services.video_metadata.extract_new_video_fields，
避免"新视频入库"和"老视频回填"两处各写一套提取逻辑。
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ScanDiscovery, ScanJob, Video
from app.services.video_metadata import extract_new_video_fields

SEED_COMMIT_BATCH_SIZE = 500


async def backfill_video_metadata(session: AsyncSession, only_missing: bool = True) -> dict:
    """
    only_missing=True(默认): 只处理 duration_seconds / collect_lon / collect_lat /
        is_stable 缺失的行，跑第二次是幂等的(已经补上的行不会重复处理)。collect_lon/lat
        缺失也要重新跑一遍的原因：扫盘时如果视频文件已经复制完但同名.srt还没
        复制完(拷贝顺序不一定同步)，当时会判定"没有srt"，GPS永远缺失——这个
        函数重新跑一遍能捕捉到"当时没有、现在已经有了"的srt文件。is_stable是
        2026-10-29新加的列，历史视频这一列普遍是NULL，自然会被这个条件捞到。
    only_missing=False: 对所有视频强制重新提取一遍(比如提取逻辑升级后想全量刷新)。

    文件在storage_path对应位置已经找不到时跳过并计数，不中断整体回填。
    """
    stmt = select(Video)
    if only_missing:
        stmt = stmt.where(
            or_(
                Video.duration_seconds.is_(None),
                Video.collect_lon.is_(None),
                Video.collect_lat.is_(None),
                Video.is_stable.is_(None),
            )
        )
    result = await session.execute(stmt)
    videos = result.scalars().all()

    checked = updated = missing_file = 0
    for video in videos:
        checked += 1
        path = Path(video.storage_path)
        if not path.is_file():
            missing_file += 1
            continue

        # extract_new_video_fields内部调用ffprobe(subprocess)+读srt文件，是
        # 同步阻塞调用，丢线程池执行，不阻塞事件循环(尤其是这个函数现在会被
        # 定时任务周期性调用，见app/main.py)
        new_fields = await asyncio.to_thread(
            extract_new_video_fields, path, settings.flight_stability_max_drift_meters
        )
        for field, value in new_fields.items():
            setattr(video, field, value)
        updated += 1

    await session.commit()
    return {"checked": checked, "updated": updated, "missing_file": missing_file}


async def seed_scan_discoveries_from_videos(session: AsyncSession) -> dict:
    """
    用videos表里已有的记录反向"灌"一份scan_discoveries缓存，只跑一次即可。

    背景：videos_only模式测试阶段扫过的视频，scan_discoveries表当时还不存在，
    从来没写过缓存记录。等scan_discoveries表建好、切到完整流程(videos_only=False)
    自动扫描时，run_scan的缓存查找(按file_path+file_size_bytes)会对这些老视频
    全部未命中，等于把已经花了一周算完的hash全部重新算一遍。

    videos表本身就存了每个视频的storage_path/file_size_bytes/sha256(当时已经
    算好的结果)，不需要重新读文件，直接用这些数据构造scan_discoveries行即可。

    幂等性：不做"已存在跳过"判断，重复跑会重复插入缓存行——不影响正确性(run_scan
    按discovered_at取最新一条)，但没必要，正常只跑一次。
    """
    job = ScanJob(
        root_path="<backfill from videos table>",
        status="completed",
        finished_at=datetime.now(timezone.utc),
    )
    session.add(job)
    await session.flush()  # 拿到job.id

    result = await session.execute(select(Video))
    videos = result.scalars().all()

    seeded = 0
    for video in videos:
        session.add(
            ScanDiscovery(
                scan_job_id=job.id,
                file_path=video.storage_path,
                file_size_bytes=video.file_size_bytes,
                hash_status="done",
                sha256=video.sha256,
                resolved_video_sha256=video.sha256,
                is_new_task=False,
            )
        )
        seeded += 1
        if seeded % SEED_COMMIT_BATCH_SIZE == 0:
            await session.commit()

    job.files_found = seeded
    await session.commit()
    return {"seeded": seeded}
