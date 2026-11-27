"""一次性脚本：给"GPS/is_stable是后来才补上的"视频补跑在线聚类算法。

背景：`scanner.py`的`_assign_city_site`(GPS+高度在线leader算法)只在视频
第一次入库那一刻跑一次——如果那一刻视频配套的.srt文件还没完整落盘，
`extract_new_video_fields`解析不出`collect_lon`/`collect_lat`，
`_assign_city_site`里"没有GPS坐标的视频不参与聚类"这条会直接跳过，
而且是永久跳过：后续`refresh_srt_fields.py`会把GPS/is_stable字段补上，
但不会反过来触发聚类，因为视频已经存在于videos表，扫描时只会走
"已存在"分支，不会再调`_apply_session_metadata`/`_assign_city_site`。

这个脚本不是给`backfill_address_clustering.py`(离线complete-linkage算法，
给系统刚上线时的历史数据一次性初始化用)补位的——这里要用的是跟生产环境
在线增量算法完全一样的那个函数(`scanner._assign_city_site`)，直接复用，
不重新实现一遍聚类逻辑，避免两套算法在同一批数据上产生不一致的判定。

用法(在aerial_backend目录下)：
    uv run python scripts/backfill_missing_location_id.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionContext  # noqa: E402
from app.models import Video  # noqa: E402
from app.services.scanner import _assign_city_site  # noqa: E402
from app.services.session_naming import find_session_match  # noqa: E402


async def main() -> None:
    async with SessionContext() as session:
        result = await session.execute(
            select(Video)
            .where(Video.deleted_at.is_(None))
            .where(Video.location_id.is_(None))
            .where(Video.location_confirmed.is_(False))
            .where(Video.city.isnot(None))
            .where(Video.collect_lon.isnot(None))
            .where(Video.collect_lat.isnot(None))
            .where(Video.is_stable.isnot(False))
            # 按入库顺序处理，跟真实扫描时的处理顺序一致——在线leader算法
            # 对处理顺序敏感(先来的视频决定新建哪些地址)
            .order_by(Video.created_at)
        )
        videos = result.scalars().all()
        print(f"找到{len(videos)}条符合条件(有GPS+city+非unstable但location_id为空)的视频")

        skipped_no_match = 0
        assigned = 0
        for video in videos:
            match = find_session_match(video.storage_path)
            if match is None:
                # 理论上不该发生——city字段本来就是从这个正则解析出来的，
                # 能有city说明当初一定匹配过；留这个分支只是防御性的
                skipped_no_match += 1
                continue
            raw_site = match.group("site")
            await _assign_city_site(session, video, video.city, raw_site)
            if video.location_id is not None:
                assigned += 1

        await session.commit()
        print(f"补上location_id: {assigned}条")
        if skipped_no_match:
            print(f"解析不出raw_site、跳过: {skipped_no_match}条(不应该出现，出现了需要人工看一下)")


if __name__ == "__main__":
    asyncio.run(main())
