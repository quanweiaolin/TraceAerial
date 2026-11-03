"""给city/device_id/location_id是空的活记录，重新尝试一次会话信息提取。

scanner.py的_apply_session_metadata只在"新插入视频"那一刻调用一次，
之后即使SESSION_FOLDER_DEVICES白名单改了(比如2026-11-03加的
mavic4pro/mini5pro)，已经入库的旧记录也不会自动重新提取——那些视频
再被扫到，走的是"sha256已存在"分支，不会重新跑元数据提取。这个脚本
补这一刀：对city IS NULL的活记录，用现在的(可能已经更新过的)白名单
重新试一次find_session_match，能匹配上就补全city/flight_height/
device_id/collected_by/location_id。

只处理"现在能匹配上"的记录——如果还是匹配不上(比如真的是历史数据，
本来就不属于这套命名规范，ADSafety那批就是)，保持原样，不是bug。

用法：
    uv run python aerial_backend/scripts/backfill_session_metadata.py \
        --database-url postgresql://user:pass@localhost:5432/aerial_db
    # 默认dry-run(只打印会改什么，不写库)，确认没问题后加 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Video
from app.services.scanner import _apply_session_metadata
from app.services.session_naming import find_session_match


async def main(database_url: str, apply: bool) -> None:
    # SQLAlchemy的create_async_engine要求postgresql+asyncpg://这个驱动前缀，
    # 跟psql/原始asyncpg.connect()能直接用的postgresql://不一样，这里兼容一下
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+asyncpg://" + database_url[len("postgresql://"):]
    engine = create_async_engine(
        database_url, connect_args={"server_settings": {"search_path": "aerial,public"}}
    )
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as session:
        result = await session.execute(
            select(Video).where(Video.city.is_(None)).where(Video.deleted_at.is_(None))
        )
        candidates = result.scalars().all()
        print(f"待检查: {len(candidates)} 条(city IS NULL的活记录)")

        matched = 0
        for video in candidates:
            path = Path(video.storage_path)
            match = find_session_match(path)
            if match is None:
                continue
            matched += 1
            print(
                f"[backfill_session_metadata] 匹配上: {video.storage_path}  "
                f"city={match.group('city')} drone={match.group('drone')}"
            )
            if apply:
                await _apply_session_metadata(session, video, path)

        print(f"\n共{len(candidates)}条，现在能匹配上的有 {matched} 条"
              f"{'，已写入' if apply else '(dry-run，没有写入，确认没问题后加 --apply 重新跑一遍)'}")
        if apply:
            await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true", help="不加这个参数只是dry-run，确认没问题后加这个才真的写入")
    args = parser.parse_args()
    asyncio.run(main(args.database_url, args.apply))
