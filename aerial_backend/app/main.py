"""
应用入口。
  - 挂FastAPI路由
  - 用APScheduler跑几个后台定时任务：
      1) 定时扫盘 (可以设成每天凌晨/每小时，看数据产生频率)
      2) 定时发送 notifications 队列里的飞书消息 (比如每分钟)
      3) 每天一次的数据库汇总(新增视频数/短视频/缺经纬度)
      4) 每天一次的VRU检测QA批次(最新一天collect的视频)，有新一批就发飞书通知
      5) 每天一次的元数据补全(duration/GPS缺失的视频重新提取一遍，主要用来
         补"扫盘时SRT文件还没复制完"导致的GPS缺失)
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.api.routers import videos
from app.config import settings
from app.db import SessionContext
from app.services.backfill import backfill_video_metadata
from app.services.notifier import process_pending_notifications, send_daily_summary
from app.services.scanner import run_scan
from app.services.vru_detection import (
    enqueue_qa_batch_notification,
    run_detection_batch,
    select_candidate_videos,
)

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


async def _run_qa_batch(collect_date: date | None, only_latest_date: bool, notify: bool) -> dict:
    """定时任务和手动接口共用这一份，避免两处各写一遍"选候选->跑检测->发通知"逻辑。"""
    if not settings.vru_model_path:
        # 模型依赖/路径还没确认好，先不报错，跳过即可，等配好AERIAL_VRU_MODEL_PATH再生效
        return {"skipped": "vru_model_path not configured"}

    async with SessionContext() as session:
        candidates = await select_candidate_videos(
            session,
            collect_date=collect_date,
            only_latest_date=only_latest_date,
        )
        if not candidates:
            return {"processed": 0, "failed": [], "results": []}

        # run_detection_batch是同步阻塞函数(跑模型推理)，直接await会卡住整个
        # asyncio事件循环——扫盘/飞书通知等其他定时任务、别的HTTP请求都会被卡住
        # 直到这批跑完。丢进线程池执行，不阻塞事件循环。
        stats = await asyncio.to_thread(
            run_detection_batch,
            candidates,
            model_path=settings.vru_model_path,
            model_name=settings.vru_model_name,
            model_version=settings.vru_model_version,
        )
        if notify and stats["processed"] > 0:
            await enqueue_qa_batch_notification(session, stats["processed"])
        return stats


async def scheduled_qa_batch() -> None:
    """每天一次，只看"最新一天collect"的视频，跑完有结果就发飞书提醒去标注。"""
    await _run_qa_batch(collect_date=None, only_latest_date=True, notify=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(scheduled_scan, "interval", hours=1, id="scan")
    scheduler.add_job(scheduled_notify, "interval", minutes=1, id="notify")
    # 每天早上9点发一次汇总；具体统计口径见 services/notifier.send_daily_summary，
    # 后续要加新指标只用改那个函数，这里的调度不用动
    scheduler.add_job(scheduled_daily_summary, "cron", hour=9, minute=0, id="daily_summary")
    # 放在daily_summary之前跑，避免俩任务撞同一分钟
    scheduler.add_job(scheduled_qa_batch, "cron", hour=8, minute=30, id="qa_batch")
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


@app.post("/qa/run-batch")
async def trigger_qa_batch(
    collect_date: str | None = None,
    only_latest_date: bool = False,
    notify: bool = True,
):
    """手动触发VRU检测QA批次，主要给前端"清洗已有数据"用：默认(only_latest_date=False)
    不限日期，把所有还没生成Test文件夹的视频都补一遍；传collect_date(如"2026-06-13")
    可以只清洗指定某一天。定时任务(scheduled_qa_batch)走的是only_latest_date=True，
    这个接口默认反过来，符合"清洗存量"这个用途。
    """
    parsed_date = date.fromisoformat(collect_date) if collect_date else None
    return await _run_qa_batch(
        collect_date=parsed_date, only_latest_date=only_latest_date, notify=notify
    )
