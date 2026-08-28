"""
应用入口。
  - 挂FastAPI路由
  - 用APScheduler跑两个后台定时任务：
      1) 定时扫盘 (可以设成每天凌晨/每小时，看数据产生频率)
      2) 定时发送 notifications 队列里的飞书消息 (比如每分钟)
"""
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.api.routers import videos
from app.config import settings
from app.db import SessionContext
from app.services.notifier import process_pending_notifications
from app.services.scanner import run_scan

scheduler = AsyncIOScheduler()


async def scheduled_scan() -> None:
    async with SessionContext() as session:
        for root in settings.scan_roots:
            await run_scan(session, root)


async def scheduled_notify() -> None:
    async with SessionContext() as session:
        await process_pending_notifications(session)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(scheduled_scan, "interval", hours=1, id="scan")
    scheduler.add_job(scheduled_notify, "interval", minutes=1, id="notify")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Aerial Video Data Management", lifespan=lifespan)
app.include_router(videos.router)


@app.post("/scan/trigger")
async def trigger_scan_now(root_path: str, videos_only: bool = False):
    """手动触发一次扫盘，不用等定时任务，方便调试/临时导入一批数据。

    videos_only=True: 数据库还没建全表(scan_jobs/scan_discoveries/notifications)时，
    只写videos表用于测试，见 services/scanner.run_scan 的说明。
    """
    async with SessionContext() as session:
        job = await run_scan(session, root_path, videos_only=videos_only)
    return {
        "job_id": job.id,
        "files_found": job.files_found,
        "files_new": job.files_new,
        "files_duplicate": job.files_duplicate,
    }
