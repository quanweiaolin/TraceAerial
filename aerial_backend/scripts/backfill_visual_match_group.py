"""一次性脚本：对某一组被GPS+高度聚类拆散的location，做视觉比对backfill。

背景：location 22/23/24几乎是同一个点(水平GPS几米量级差异)，但因为
srt_height差异超过了聚类算法30m的3D距离阈值，被在线leader算法拆成了
3个不同的location——目前42个生产location没有任何一个做过视觉校验
(location_review_items表是空的)，这3个也不例外。

只抽视频的第一帧有风险：起飞/降落/收放起落架那几秒也会被拍进去，
单应矩阵一旦比对的是这种镜头，SIFT角点稳不稳跟"是不是同一个地方"这个
问题完全脱钩。所以这里对每个视频抽首/中/尾三帧，三组时间点分别两两
比对——如果三组结果互相矛盾(比如首帧配上、尾帧配不上)，说明至少有一帧
拍到了不该用来判断的镜头，需要人工盯着frames目录里的图自己看是哪一帧
出的问题，不能只看单应矩阵的数字。

这个脚本不进正常复核队列(不建LocationReviewItem行，不碰
representative_frame_path/representative_video_sha256)，只是打印三组
SIFT+RANSAC的结果给人工判断提供证据，不写任何DB——是否要把这组重新塞进
复核队列让人正式决策，是下一步、需要单独确认的动作。

用法(在aerial_backend目录下)：
    uv run python scripts/backfill_visual_match_group.py --location-ids 22 23 24

跑完后三帧的截图留在打印出来的那个临时目录里，没有清理，方便你直接去看
是不是真的拍到了起飞/降落。
"""
import argparse
import asyncio
import itertools
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionContext  # noqa: E402
from app.models import Location, Video  # noqa: E402
from app.services.location_visual_match import compare_frames  # noqa: E402
from app.services.video_metadata import probe_video  # noqa: E402

# 跟compare_frames同一批视频里挑首/中/尾三个时刻，而不是只看第一帧，
# 因为第一帧很容易拍到起飞/收放起落架这种跟"是不是同一个地方"无关的镜头
_FRAME_POSITIONS = ("first", "middle", "last")


def extract_frame_at(video_path: str, timestamp_seconds: float, output_path: str) -> None:
    """跟location_visual_match.extract_frame同一套subprocess惯例，只是
    加了-ss定位到指定秒数，不改共享的生产函数(这是一次性脚本专用的小变体)。"""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(max(timestamp_seconds, 0.0)),
                "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"截图提取失败: {exc}") from exc

    if proc.returncode != 0:
        raise ValueError(f"截图提取失败: {proc.stderr}")

    if not Path(output_path).is_file():
        raise ValueError(f"截图提取失败: {proc.stderr}")


async def pick_representative_video(session, location_id: int) -> Video | None:
    result = await session.execute(
        select(Video).where(Video.location_id == location_id).limit(1)
    )
    return result.scalars().first()


async def main(location_ids: list[int]) -> None:
    async with SessionContext() as session:
        # frames[location_id][position] = 截图路径
        frames: dict[int, dict[str, str]] = {}
        tmp_dir = tempfile.mkdtemp(prefix="visual_match_backfill_")
        print(f"截图保存在: {tmp_dir}（脚本结束后不会自动清理）")
        print()

        for location_id in location_ids:
            location = await session.get(Location, location_id)
            if location is None:
                print(f"location id={location_id} 不存在，跳过")
                continue

            video = await pick_representative_video(session, location_id)
            if video is None:
                print(f"location id={location_id}({location.city}/{location.site}) 名下没有视频，跳过")
                continue

            duration = probe_video(video.storage_path)["duration_seconds"]
            if not duration:
                print(f"location id={location_id} 读不到视频时长，跳过")
                continue

            timestamps = {
                "first": 0.0,
                "middle": duration / 2,
                "last": max(duration - 1.0, 0.0),
            }

            frames[location_id] = {}
            for position in _FRAME_POSITIONS:
                frame_path = str(Path(tmp_dir) / f"location_{location_id}_{position}.jpg")
                try:
                    extract_frame_at(video.storage_path, timestamps[position], frame_path)
                except ValueError as exc:
                    print(f"location id={location_id} 抽{position}帧失败: {exc}")
                    continue
                frames[location_id][position] = frame_path

            print(
                f"location id={location_id}({location.city}/{location.site}) "
                f"用视频 {video.storage_path}（时长{duration:.0f}s）抽了"
                f"{list(frames[location_id].keys())}帧"
            )

        print()
        print("两两视觉比对结果(按首/中/尾分别比)：")
        for id_a, id_b in itertools.combinations(frames.keys(), 2):
            print(f"  location {id_a} vs location {id_b}:")
            for position in _FRAME_POSITIONS:
                if position not in frames[id_a] or position not in frames[id_b]:
                    print(f"    {position}: 跳过(缺这一帧)")
                    continue
                verdict = compare_frames(frames[id_a][position], frames[id_b][position])
                print(
                    f"    {position}: matched={verdict['matched']} "
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
