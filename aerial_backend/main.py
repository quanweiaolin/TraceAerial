import asyncio
import sys

from app.db import SessionContext
from app.services.backfill import backfill_video_metadata
from app.services.scanner import run_scan


async def test_scan_videos_only(root_path: str) -> None:
    async with SessionContext() as session:
        job = await run_scan(session, root_path, videos_only=True)
    print(f"found={job.files_found} new={job.files_new} duplicate={job.files_duplicate}")


async def test_backfill(force: bool = False) -> None:
    async with SessionContext() as session:
        stats = await backfill_video_metadata(session, only_missing=not force)
    print(f"checked={stats['checked']} updated={stats['updated']} missing_file={stats['missing_file']}")


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run main.py <root_path_to_scan>")
        print("       uv run main.py --backfill [--force]")
        return
    if sys.argv[1] == "--backfill":
        asyncio.run(test_backfill(force="--force" in sys.argv))
    else:
        asyncio.run(test_scan_videos_only(sys.argv[1]))


if __name__ == "__main__":
    main()
