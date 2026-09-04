import asyncio
import sys

from app.db import SessionContext
from app.services.backfill import backfill_video_metadata, seed_scan_discoveries_from_videos
from app.services.scanner import run_scan


async def test_scan_videos_only(root_path: str) -> None:
    async with SessionContext() as session:
        job = await run_scan(session, root_path, videos_only=True)
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
        asyncio.run(test_scan_videos_only(sys.argv[1]))


if __name__ == "__main__":
    main()
