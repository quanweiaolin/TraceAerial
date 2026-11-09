"""
应用入口。
  - 挂FastAPI路由
  - 用APScheduler跑几个后台定时任务：
      1) 定时扫盘 (可以设成每天凌晨/每小时，看数据产生频率)
      2) 定时发送 notifications 队列里的飞书消息 (比如每分钟)
      3) 每天一次的数据库汇总(新增视频数/短视频/缺经纬度)
      4) 每天一次的元数据补全(duration/GPS缺失的视频重新提取一遍，主要用来
         补"扫盘时SRT文件还没复制完"导致的GPS缺失)
      5) 每3天一次的本地文件完整性检查(videos表活记录对应的本地文件是不是
         真的还在，不限于走阿里云同步进来的那部分；发现缺失只按文件夹粒度
         报一条飞书，不自动修复)
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.api.routers import aliyun_status, aliyun_urgent_download, videos
from app.config import settings
from app.db import SessionContext
from app.services import feishu_bot
from app.services.aliyun_sync import ALIYUN_SCAN_INTERVAL_HOURS, run_aliyun_scan
from app.services.backfill import backfill_video_metadata
from app.services.notifier import process_pending_notifications, send_daily_summary
from app.services.scanner import check_local_file_integrity, run_scan

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


async def scheduled_aliyun_scan() -> None:
    async with SessionContext() as session:
        await run_aliyun_scan(session)


async def scheduled_metadata_backfill() -> None:
    """跑在daily_summary之前，让当天被SRT晚到影响的"缺经纬度"视频有机会在
    汇总统计前补上，daily_summary里的缺经纬度数字会因此自然变小。"""
    async with SessionContext() as session:
        await backfill_video_metadata(session, only_missing=True)


async def scheduled_check_missing_files() -> None:
    async with SessionContext() as session:
        await check_local_file_integrity(session)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # interval任务默认第一次触发时间是"现在+间隔"，不是启动后马上跑——
    # 显式传next_run_time=启动时刻，让它启动后立刻跑一次，之后再按间隔重复，
    # 不用每次重启服务都傻等一整个间隔才看到第一次结果
    now = datetime.now()
    scheduler.add_job(scheduled_scan, "interval", hours=1, id="scan", next_run_time=now)
    scheduler.add_job(scheduled_notify, "interval", minutes=1, id="notify", next_run_time=now)
    # 每天早上9点发一次汇总；具体统计口径见 services/notifier.send_daily_summary，
    # 后续要加新指标只用改那个函数，这里的调度不用动
    scheduler.add_job(scheduled_daily_summary, "cron", hour=9, minute=0, id="daily_summary")
    # scheduler.add_job(scheduled_daily_summary, "interval", minutes=1, id="daily_summary")
    # 放在daily_summary前，让当天GPS缺失的视频有机会先补上再统计
    scheduler.add_job(
        scheduled_metadata_backfill, "cron", hour=8, minute=45, id="metadata_backfill"
    )
    scheduler.add_job(
        scheduled_aliyun_scan, "interval", hours=ALIYUN_SCAN_INTERVAL_HOURS,
        id="aliyun_scan", next_run_time=now,
    )
    # 每3天检查一次本地文件完整性(videos表活记录对应的文件是不是真的还在磁盘
    # 上)，next_run_time=now让每次服务重启都立刻跑一次，不用傻等一整个间隔
    # (2026-11-09: 发现这里之前漏了next_run_time=now，导致重启后要等满7天
    # 才跑第一次，南宁xuchaoshun文件夹的"本地文件被覆盖"问题因此一直没被
    # 这个检查发现过；顺手把间隔从7天缩到3天)
    scheduler.add_job(
        scheduled_check_missing_files, "interval", days=3,
        id="check_missing_files", next_run_time=now,
    )
    scheduler.start()
    # 飞书LLM问答机器人(长连接)：传当前正在跑的主loop进去，因为这个机器人
    # 在自己专门开的线程里跑，查数据库/调agent那部分要弹回这个主loop执行，
    # 见services/feishu_bot.py模块docstring
    feishu_bot.start(asyncio.get_running_loop())
    yield
    scheduler.shutdown()


app = FastAPI(title="Aerial Video Data Management", lifespan=lifespan)
app.include_router(videos.router)
app.include_router(aliyun_status.router)
app.include_router(aliyun_urgent_download.router)


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
