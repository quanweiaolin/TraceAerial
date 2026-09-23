"""
手动跑一次阿里云PDS扫描，方便测试(而不是等APScheduler的6小时定时任务)。
不改动任何调度逻辑，就是直接调run_aliyun_scan一次。

用法(在aerial_backend目录下)：
    uv run python scripts/run_aliyun_scan_once.py
"""
import asyncio

from app.db import SessionContext
from app.services.aliyun_sync import run_aliyun_scan


async def main() -> None:
    async with SessionContext() as session:
        await run_aliyun_scan(session)
    print("扫描完成，结果见 aliyun_folder_scans / aliyun_sync_events 表")


if __name__ == "__main__":
    asyncio.run(main())
