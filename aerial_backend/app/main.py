"""
应用入口。
  - 挂FastAPI路由
  - 用APScheduler跑几个后台定时任务：
      1) 定时扫盘 (可以设成每天凌晨/每小时，看数据产生频率)
      2) 定时发送 notifications 队列里的飞书消息 (比如每分钟)
      3) 每天一次的数据库汇总(新增视频数/短视频/缺经纬度)
      4) 每天一次的元数据补全(duration/GPS缺失的视频重新提取一遍，主要用来
         补"扫盘时SRT文件还没复制完"导致的GPS缺失)
"""
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.api.routers import videos
from app.config import settings
from app.db import SessionContext
from app.services.backfill import backfill_video_metadata
from app.services.notifier import process_pending_notifications, send_daily_summary
from app.services.scanner import run_scan

scheduler = AsyncIOScheduler()


async def scheduled_scan() -> None:
    async with SessionContext() as session:
        for root in settings.scan_roots:
            await run_scan(session, root)


async def scheduled_notify() -> None:
    async with SessionContext() as session:
        await process_pending_notifications(session)


async def scheduled_daily_summary() -> None:
    async with SessionContext() as session:
        await send_daily_summary(session)


async def scheduled_metadata_backfill() -> None:
    """跑在daily_summary之前，让当天被SRT晚到影响的"缺经纬度"视频有机会在
    汇总统计前补上，daily_summary里的缺经纬度数字会因此自然变小。"""
    async with SessionContext() as session:
        await backfill_video_metadata(session, only_missing=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(scheduled_scan, "interval", hours=1, id="scan")
    # scheduler.add_job(scheduled_notify, "interval", hours=1, id="notify")
    # 每天早上9点发一次汇总；具体统计口径见 services/notifier.send_daily_summary，
    # 后续要加新指标只用改那个函数，这里的调度不用动
    scheduler.add_job(scheduled_daily_summary, "cron", hour=9, minute=0, id="daily_summary")
    # scheduler.add_job(scheduled_daily_summary, "interval", minutes=1, id="daily_summary")
    # 放在daily_summary前，让当天GPS缺失的视频有机会先补上再统计
    scheduler.add_job(
        scheduled_metadata_backfill, "cron", hour=8, minute=45, id="metadata_backfill"
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Aerial Video Data Management", lifespan=lifespan)
app.include_router(videos.router)


@app.post("/scan/trigger")
async def trigger_scan_now(root_path: str):
    """手动触发一次扫盘，不用等定时任务，方便调试/临时导入一批数据。"""
    async with SessionContext() as session:
        job = await run_scan(session, root_path)
    return {
        "job_id": job.id,
        "files_found": job.files_found,
        "files_new": job.files_new,
        "files_duplicate": job.files_duplicate,
    }
