"""一次性backfill脚本：对所有city扫一遍命名审核告警。

这个脚本会写数据库，插入location_naming_alerts记录(不改Location/Video的任何现有数据)。
按项目约定，这个脚本交给你自己在生产机上跑，不由AI代跑。

用法(在aerial_backend目录下)：
    uv run python scripts/backfill_location_naming_audit.py
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionContext  # noqa: E402
from app.models import Location  # noqa: E402
from app.services.location_naming_audit import detect_naming_alerts  # noqa: E402


async def main() -> None:
    async with SessionContext() as session:
        # 查所有不同的city
        result = await session.execute(select(func.distinct(Location.city)))
        cities = [row[0] for row in result]

        total_inserted = 0
        for city in cities:
            inserted = await detect_naming_alerts(session, city)
            total_inserted += inserted

        await session.commit()

        print(f"扫了{len(cities)}个city，生成了{total_inserted}条新提示")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    asyncio.run(main())
