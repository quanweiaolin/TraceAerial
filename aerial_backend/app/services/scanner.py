"""
扫盘服务。

流程：
  1. 遍历 root_path 下的视频文件 -> 写 scan_discoveries(pending)
  2. 逐个算sha256 -> 更新 hash_status=done, 填sha256
  3. 查 videos 表：
     - 不存在该sha256 -> 这是"新数据" -> INSERT videos，is_new_task=True，
       并给 notifications 插一条 new_task 通知
     - 已存在 -> 只关联 resolved_video_sha256，is_new_task=False，不重复建任务
       (同一内容换了路径/被复制，不应该产生第二条任务流)

可以由 APScheduler 定时调用，也可以挂在 FastAPI 的 POST /scan/trigger 接口里异步跑。
"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, ScanDiscovery, ScanJob, Video
from app.services.hashing import compute_sha256

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}


async def run_scan(session: AsyncSession, root_path: str, videos_only: bool = False) -> ScanJob:
    """
    videos_only=True: 【临时测试开关】数据库还没建全表(scan_jobs/scan_discoveries/
    notifications)时，手动测扫盘只写videos表用。
    效果：ScanJob/ScanDiscovery/Notification这三类对象只在内存里用来算计数，
    不调用session.add()，commit时就不会往这3张(还没建的)表写数据；
    只有Video对象会被add()，最终只有videos表会被真正落库。

    等这3张表建好后，调用方直接不传这个参数(默认False)即可恢复完整流程，
    这段代码不用再改。
    """
    job = ScanJob(root_path=root_path)
    if not videos_only:
        session.add(job)
        await session.flush()  # 拿到job.id

    files_found = files_new = files_duplicate = 0

    for path in Path(root_path).rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        files_found += 1

        discovery = None
        if not videos_only:
            discovery = ScanDiscovery(
                scan_job_id=job.id,
                file_path=str(path),
                file_size_bytes=path.stat().st_size,
                hash_status="hashing",
            )
            session.add(discovery)
            await session.flush()

        try:
            digest = compute_sha256(path)
        except OSError as e:
            if discovery is not None:
                discovery.hash_status = "error"
                discovery.error_message = str(e)
            continue

        if discovery is not None:
            discovery.sha256 = digest
            discovery.hash_status = "done"

        existing = await session.get(Video, digest)
        if existing is None:
            # 真新增：insert videos 会触发DB里 trg_init_pipeline_status(若已建)，
            # 自动为它生成8个阶段的pending状态行；触发器还没建时这一步只是单纯insert
            video = Video(
                sha256=digest,
                original_name=path.name,
                storage_path=str(path),
                file_size_bytes=path.stat().st_size,
            )
            session.add(video)
            files_new += 1
            if discovery is not None:
                discovery.is_new_task = True
            if not videos_only:  # notifications表未建时跳过，videos_only模式下不发通知
                session.add(
                    Notification(
                        video_sha256=digest,
                        event_type="new_task",
                        channel="feishu",
                        payload={"file_path": str(path), "original_name": path.name},
                    )
                )
        else:
            files_duplicate += 1
            if discovery is not None:
                discovery.is_new_task = False

        if discovery is not None:
            discovery.resolved_video_sha256 = digest

    job.files_found = files_found
    job.files_new = files_new
    job.files_duplicate = files_duplicate
    job.status = "completed"
    from datetime import datetime, timezone
    job.finished_at = datetime.now(timezone.utc)

    await session.commit()
    return job
