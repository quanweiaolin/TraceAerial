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


async def run_scan(session: AsyncSession, root_path: str) -> ScanJob:
    job = ScanJob(root_path=root_path)
    session.add(job)
    await session.flush()  # 拿到job.id

    files_found = files_new = files_duplicate = 0

    for path in Path(root_path).rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        files_found += 1

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
            discovery.hash_status = "error"
            discovery.error_message = str(e)
            continue

        discovery.sha256 = digest
        discovery.hash_status = "done"

        existing = await session.get(Video, digest)
        if existing is None:
            # 真新增：insert videos 会触发DB里 trg_init_pipeline_status，
            # 自动为它生成8个阶段的pending状态行
            video = Video(
                sha256=digest,
                original_name=path.name,
                storage_path=str(path),
                file_size_bytes=discovery.file_size_bytes,
            )
            session.add(video)
            discovery.is_new_task = True
            files_new += 1

            session.add(
                Notification(
                    video_sha256=digest,
                    event_type="new_task",
                    channel="feishu",
                    payload={"file_path": str(path), "original_name": path.name},
                )
            )
        else:
            discovery.is_new_task = False
            files_duplicate += 1

        discovery.resolved_video_sha256 = digest

    job.files_found = files_found
    job.files_new = files_new
    job.files_duplicate = files_duplicate
    job.status = "completed"
    from datetime import datetime, timezone
    job.finished_at = datetime.now(timezone.utc)

    await session.commit()
    return job
