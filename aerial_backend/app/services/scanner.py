"""
扫盘服务。

流程：
  1. 遍历 root_path 下的视频文件 -> 写 scan_discoveries(pending)
  2. 逐个算sha256 -> 更新 hash_status=done, 填sha256
  3. 查 videos 表：
     - 不存在该sha256:
         - 同一storage_path没有活着的旧记录 -> 真新视频 -> INSERT videos，
           is_new_task=True，发 new_task 即时通知
         - 同一storage_path有活着的旧记录(sha256不同) -> 原地重新编码(长期瘦身
           转码任务)，不是新视频 -> 旧记录deleted_at软删除下线，新记录
           replaces_sha256指回旧sha256，不发即时通知(走每日汇总统计)
     - 已存在 -> 只关联 resolved_video_sha256，is_new_task=False，不重复建任务
       (同一内容换了路径/被复制，不应该产生第二条任务流)

可以由 APScheduler 定时调用，也可以挂在 FastAPI 的 POST /scan/trigger 接口里异步跑。
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, ScanDiscovery, ScanJob, Video
from app.services.hashing import compute_sha256
from app.services.video_metadata import extract_new_video_fields

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}
COMMIT_BATCH_SIZE = 100  # 每处理这么多个文件落库一次，避免大目录跑到一半被杀/OOM全部白扫


async def run_scan(session: AsyncSession, root_path: str) -> ScanJob:
    """
    完整流程，需要scan_jobs/scan_discoveries/notifications三张表已建好(建表SQL见
    enable_scan_pipeline.sql)。

    ------------------------------------------------------------------
    【历史记录，供以后需要时参考，不是活代码】
    早期这几张表还没建的阶段，这个函数有一个 videos_only: bool = False 参数：
    传True时，ScanJob/ScanDiscovery/Notification只在内存里用来算计数、不
    session.add()，commit时就只有Video会真正落库，用于绕开还没建的表手动测
    "扫盘写videos表"这一步。三张表建好、切回默认参数后就不再需要了。
    如果以后又出现"部分表还没建，想只测videos这一步"的场景，可以从git历史
    找回这段逻辑：`git show 08b1c36:aerial_backend/app/services/scanner.py`
    (或更早的 70162d1/2c04b21)。
    ------------------------------------------------------------------
    """
    job = ScanJob(root_path=root_path)
    session.add(job)
    await session.flush()  # 拿到job.id

    files_found = files_new = files_duplicate = 0

    for path in Path(root_path).rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if path.name.startswith("._"):
            # macOS拷贝到非HFS+文件系统(NAS的exFAT/SMB/NFS)时自动生成的
            # AppleDouble元数据"影子"文件，体积很小(常见4096字节)，跟真实
            # 视频内容无关，不是"正在上传中"——直接跳过，不算files_found
            continue
        if ".compress_tmp." in path.name:
            # 视频瘦身(重新编码)转码过程中的临时输出文件，转码完成后会被
            # 重命名/清理掉，扫盘时如果恰好撞见半成品不应该当成正式视频记录
            continue
        files_found += 1
        # print(f"[scan] scanning: {path}", flush=True)
        file_size = path.stat().st_size

        # 缓存命中：之前扫过同路径+同大小的文件、且hash算完了，直接复用sha256，
        # 不重新读整个文件。判断依据只用路径+大小，不含mtime——够用且没有额外
        # 表结构成本，适合"归档只增不改"的场景；如果文件曾被原地替换成同样大小
        # 的不同内容，这里会误判成"没变"，目前认为概率低到可以接受。
        cache_result = await session.execute(
            select(ScanDiscovery.sha256)
            .where(ScanDiscovery.file_path == str(path))
            .where(ScanDiscovery.file_size_bytes == file_size)
            .where(ScanDiscovery.hash_status == "done")
            .order_by(ScanDiscovery.discovered_at.desc())
            .limit(1)
        )
        cached_sha256 = cache_result.scalar_one_or_none()

        discovery = ScanDiscovery(
            scan_job_id=job.id,
            file_path=str(path),
            file_size_bytes=file_size,
            hash_status="done" if cached_sha256 is not None else "hashing",
            sha256=cached_sha256,
        )
        session.add(discovery)
        await session.flush()

        if cached_sha256 is not None:
            digest = cached_sha256
        else:
            try:
                # compute_sha256是同步阻塞IO(读整个视频文件)，直接调用会卡住
                # 整个asyncio事件循环，导致同一进程里其他协程(比如每分钟一次的
                # scheduled_notify)被拖慢甚至"missed"。丢进线程池执行。
                digest = await asyncio.to_thread(compute_sha256, path)
            except OSError as e:
                discovery.hash_status = "error"
                discovery.error_message = str(e)
                continue

            discovery.sha256 = digest
            discovery.hash_status = "done"

        existing = await session.get(Video, digest)
        if existing is None:
            # 新sha256，但可能不是"全新视频"——如果同一个storage_path上已经有
            # 一条还活着(deleted_at IS NULL)的旧记录，说明是原地重新编码(长期
            # 瘦身转码任务)覆盖了同一份文件，不是真新视频。videos.storage_path
            # 上有WHERE deleted_at IS NULL的唯一索引，旧记录不软删除的话下面
            # insert会直接报错。
            old_active_result = await session.execute(
                select(Video)
                .where(Video.storage_path == str(path))
                .where(Video.deleted_at.is_(None))
            )
            old_active = old_active_result.scalar_one_or_none()
            is_re_encode = old_active is not None

            # 技术元数据(时长/分辨率/帧率/编码/collected_at)+GPS：ffprobe只读文件头，
            # 不会像sha256那样耗时；但内部调subprocess仍是阻塞调用，同样丢线程池，
            # 不占用事件循环；device_id/collected_by保持不变，本次不处理
            new_fields = await asyncio.to_thread(extract_new_video_fields, path)
            video = Video(
                sha256=digest,
                original_name=path.name,
                storage_path=str(path),
                file_size_bytes=file_size,
                replaces_sha256=old_active.sha256 if is_re_encode else None,
                **new_fields,
            )
            session.add(video)
            await session.flush()  # 必须在这里flush，确保video先落库，
            # 不然下面Notification.video_sha256(外键指向videos.sha256)可能
            # 在同一次自动flush里insert顺序不对，报FK违反(见2026-09-07的事故)
            files_new += 1
            discovery.is_new_task = True

            if is_re_encode:
                # 旧记录软删除下线，不物理删除，历史数据还在；不发即时飞书通知，
                # 走每日汇总(services/notifier.send_daily_summary)统计"今日更新了多少视频"
                old_active.deleted_at = datetime.now(timezone.utc)
            else:
                # 真新增：insert videos 会触发DB里 trg_init_pipeline_status(若已建)，
                # 自动为它生成8个阶段的pending状态行；触发器还没建时这一步只是单纯insert
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
            discovery.is_new_task = False

        discovery.resolved_video_sha256 = digest

        # 每处理满COMMIT_BATCH_SIZE个文件落库一次，而不是等全部扫完才提交：
        # 1) 大目录跑到一半被杀(OOM/tmux没了)，已经处理过的视频不会白扫
        # 2) 已提交对象会被session释放，内存不会随扫描进度一直往上涨
        job.files_found = files_found
        job.files_new = files_new
        job.files_duplicate = files_duplicate
        if files_found % COMMIT_BATCH_SIZE == 0:
            await session.commit()
            print(f"[scan] committed at files_found={files_found}", flush=True)

    job.files_found = files_found
    job.files_new = files_new
    job.files_duplicate = files_duplicate
    job.status = "completed"
    job.finished_at = datetime.now(timezone.utc)

    await session.commit()
    print(
        f"[scan] 扫描完成: root={root_path} files_found={files_found} "
        f"files_new={files_new} files_duplicate={files_duplicate}",
        flush=True,
    )
    return job
