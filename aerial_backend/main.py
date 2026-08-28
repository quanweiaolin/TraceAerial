import asyncio
import sys

from app.db import SessionContext
from app.services.scanner import run_scan


async def test_scan_videos_only(root_path: str) -> None:
    async with SessionContext() as session:
        job = await run_scan(session, root_path, videos_only=True)
    print(f"found={job.files_found} new={job.files_new} duplicate={job.files_duplicate}")


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run main.py <root_path_to_scan>")
        return
    asyncio.run(test_scan_videos_only(sys.argv[1]))


if __name__ == "__main__":
    main()
