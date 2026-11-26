"""一次性脚本：对某一组被GPS+高度聚类拆散的location，做视觉比对backfill。

背景：location 22/23/24几乎是同一个点(水平GPS几米量级差异)，但因为
srt_height差异超过了聚类算法30m的3D距离阈值，被在线leader算法拆成了
3个不同的location——目前42个生产location没有任何一个做过视觉校验
(location_review_items表是空的)，这3个也不例外。

这个脚本不进正常复核队列(不建LocationReviewItem行，不碰
representative_frame_path/representative_video_sha256)，只是从每个
location下随便挑一个视频抽帧，两两跑compare_frames，把SIFT+RANSAC的
结果打印出来，给人工判断"这几个到底是不是同一个地方"提供证据。跑完
这个之后由人根据结果去决定location name/别名怎么填，不是这个脚本该管
的事——所以特意不在这里写任何DB更新。

用法(在aerial_backend目录下)：
    uv run python scripts/backfill_visual_match_group.py --location-ids 22 23 24
"""
import argparse
import asyncio
import itertools
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.db import SessionContext
from app.models import Location, Video
from app.services.location_visual_match import compare_frames, extract_frame


async def pick_representative_video(session, location_id: int) -> Video | None:
    result = await session.execute(
        select(Video).where(Video.location_id == location_id).limit(1)
    )
    return result.scalars().first()


async def main(location_ids: list[int]) -> None:
    async with SessionContext() as session:
        frames: dict[int, str] = {}
        with tempfile.TemporaryDirectory(prefix="visual_match_backfill_") as tmp_dir:
            for location_id in location_ids:
                location = await session.get(Location, location_id)
                if location is None:
                    print(f"location id={location_id} 不存在，跳过")
                    continue

                video = await pick_representative_video(session, location_id)
                if video is None:
                    print(f"location id={location_id}({location.city}/{location.site}) 名下没有视频，跳过")
                    continue

                frame_path = str(Path(tmp_dir) / f"location_{location_id}.jpg")
                try:
                    extract_frame(video.storage_path, frame_path)
                except ValueError as exc:
                    print(f"location id={location_id} 抽帧失败: {exc}")
                    continue

                frames[location_id] = frame_path
                print(
                    f"location id={location_id}({location.city}/{location.site}) "
                    f"用视频 {video.storage_path} 抽帧 -> {frame_path}"
                )

            print()
            print("两两视觉比对结果：")
            for id_a, id_b in itertools.combinations(frames.keys(), 2):
                verdict = compare_frames(frames[id_a], frames[id_b])
                print(
                    f"  location {id_a} vs location {id_b}: "
                    f"matched={verdict['matched']} "
                    f"inliers={verdict['inliers']} "
                    f"inlier_ratio={verdict['inlier_ratio']:.3f} "
                    f"auto_accepted={verdict['auto_accepted']} "
                    f"({verdict['auto_reason']})"
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--location-ids", required=True, nargs="+", type=int)
    args = parser.parse_args()
    asyncio.run(main(args.location_ids))
