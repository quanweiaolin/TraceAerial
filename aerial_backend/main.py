import asyncio
import sys

from app.db import SessionContext
from app.services.backfill import backfill_video_metadata, seed_scan_discoveries_from_videos
from app.services.scanner import run_scan

# 历史记录，供以后需要时参考：这个文件之前有 test_scan_videos_only()/--full 两条路径，
# 分别对应 run_scan 还有个 videos_only 参数的年代(数据库表没建全时只写videos表测试)。
# 三张表(scan_jobs/scan_discoveries/notifications)建好后不再需要区分，
# 见 app/services/scanner.py 顶部注释，可从git历史(如 08b1c36)找回。


async def test_scan(root_path: str) -> None:
    async with SessionContext() as session:
        job = await run_scan(session, root_path)
    print(f"found={job.files_found} new={job.files_new} duplicate={job.files_duplicate}")


async def test_backfill(force: bool = False) -> None:
    async with SessionContext() as session:
        stats = await backfill_video_metadata(session, only_missing=not force)
    print(f"checked={stats['checked']} updated={stats['updated']} missing_file={stats['missing_file']}")


async def test_seed_cache() -> None:
    async with SessionContext() as session:
        stats = await seed_scan_discoveries_from_videos(session)
    print(f"seeded={stats['seeded']}")


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run main.py <root_path_to_scan>")
        print("       uv run main.py --backfill [--force]")
        print("       uv run main.py --seed-cache")
        return
    if sys.argv[1] == "--backfill":
        asyncio.run(test_backfill(force="--force" in sys.argv))
    elif sys.argv[1] == "--seed-cache":
        asyncio.run(test_seed_cache())
    else:
        asyncio.run(test_scan(sys.argv[1]))


if __name__ == "__main__":
    main()
