"""快速刷新collect_lon/collect_lat/srt_height/is_stable这4个SRT相关字段，
不碰ffprobe技术元数据(duration/width/height/fps/codec/collected_at)。

背景：已有的全量回填入口`main.py --backfill --force`
(app.services.backfill.backfill_video_metadata(only_missing=False))对
每条视频都会调用ffprobe起子进程读视频文件头，NAS上几万条视频跑起来很慢；
但这次要刷新的collect_lon/collect_lat/srt_height只是改了提取逻辑(取
全程中位数，不是取第一条)、新加的is_stable纯粹是读同一份SRT算出来的，
根本不需要碰视频文件本体/ffprobe。这个脚本只读SRT文本文件(本身很小)，
跳过ffprobe，专门给"只想刷这4个字段，不想为了技术元数据重新扫一遍全部
视频文件"的场景用，比全量回填快得多。

只处理 deleted_at IS NULL(还活着) 的视频；每条都会尝试重新解析(不是只
处理4个字段里有NULL的——这次要刷的就是"已经有值但值可能不准"的旧数据，
按NULL筛选反而会漏掉这些)。sidecar SRT文件找不到(被挪动/清理过/这条
视频本来就没有SRT)时保留DB里原有的值，不会清空成NULL——避免因为这次跑
的时候偶然读不到文件(比如NAS抖动)把原本有效的字段冲掉。

并发：读SRT(find_srt_sidecar的stat调用+读文件内容)在NAS上是网络IO，不是
CPU，一条一条串行跑的话总耗时=23844次网络往返时间的总和，哪怕每次只要
50ms也是接近20分钟起步(2026-10-29生产端实测确认过串行版本慢得不可用)。
这里用--concurrency个线程同时读(asyncio.to_thread丢进线程池，IO在等待
网络的时候不占CPU，可以大量并发)，数据库写入仍然是单连接顺序执行——写
一行是本地postgres操作，不是瓶颈，没必要为此搞连接池。

用法：
    uv run python aerial_backend/scripts/refresh_srt_fields.py \
        --database-url postgresql://user:pass@localhost:5432/aerial_db \
        --max-drift-meters 30 --concurrency 32
    # 不加--apply是dry-run：只读文件解析、打印统计，不写库。
    # --max-drift-meters要跟.env里AERIAL_FLIGHT_STABILITY_MAX_DRIFT_METERS
    # 一致(默认30)，这里单独传参是为了这个脚本不依赖app.config/.env，
    # 避免cwd不对导致.env读不到的问题(跟backfill_video_sha1.py同样的考虑)。
    # --concurrency太大可能把NAS打满导致其他服务变慢，先用默认32试，
    # 不够快可以往上调，NAS扛不住就往下调
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.video_metadata import extract_srt_fields, find_srt_sidecar  # noqa: E402

PROGRESS_EVERY = 500


def _read_one(path: str, max_drift_meters: float) -> dict | None:
    """在线程池里跑的部分：find_srt_sidecar本身也是阻塞的文件系统调用
    (stat)，之前版本漏了没丢进线程池，相当于主事件循环也在等这个——这次
    一起放进同一个线程任务里，配合外层的并发度控制。"""
    srt_path = find_srt_sidecar(path)
    if srt_path is None:
        return None
    return extract_srt_fields(path, max_drift_meters)


async def main(
    database_url: str, apply: bool, max_drift_meters: float, limit: int | None, concurrency: int
) -> None:
    conn = await asyncpg.connect(database_url, server_settings={"search_path": "aerial,public"})
    try:
        query = "SELECT sha256, storage_path FROM videos WHERE deleted_at IS NULL ORDER BY sha256"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = await conn.fetch(query)

        total = len(rows)
        print(f"待处理: {total} 条(deleted_at IS NULL的活记录，不限于字段缺失的)，并发度={concurrency}")
        if total == 0:
            return

        semaphore = asyncio.Semaphore(concurrency)
        done_count = 0

        async def process(row) -> tuple:
            nonlocal done_count
            async with semaphore:
                fields = await asyncio.to_thread(_read_one, row["storage_path"], max_drift_meters)
            done_count += 1
            if done_count % PROGRESS_EVERY == 0:
                print(f"[refresh_srt] 进度 {done_count}/{total}", flush=True)
            return row["sha256"], fields

        # gather一次性提交全部任务，靠Semaphore控制实际同时跑的数量，不是
        # 分批提交——这样IO slot一空出来就立刻补下一个任务，吞吐量最大
        results = await asyncio.gather(*(process(r) for r in rows))

        no_srt = updated_gps = updated_height = updated_stability = 0
        for sha256, fields in results:
            if fields is None:
                no_srt += 1
                continue
            if fields["collect_lon"] is not None:
                updated_gps += 1
            if fields["srt_height"] is not None:
                updated_height += 1
            if fields["is_stable"] is not None:
                updated_stability += 1

            if apply:
                # COALESCE：这次解析不出来的字段(比如这次run恰好srt读失败、
                # 或者is_stable因为是老格式永久解析不出轨迹)保留DB里的旧值，
                # 不要用None把原本有效的值冲掉
                await conn.execute(
                    "UPDATE videos SET "
                    "collect_lon = COALESCE($2, collect_lon), "
                    "collect_lat = COALESCE($3, collect_lat), "
                    "srt_height = COALESCE($4, srt_height), "
                    "is_stable = COALESCE($5, is_stable) "
                    "WHERE sha256 = $1",
                    sha256, fields["collect_lon"], fields["collect_lat"],
                    fields["srt_height"], fields["is_stable"],
                )

        print(
            f"\n完成: 共{total}条，没有sidecar SRT跳过(字段保持原值) {no_srt} 条；"
            f"{'已更新' if apply else '将更新(dry-run)'}: "
            f"GPS {updated_gps} 条，srt_height {updated_height} 条，is_stable {updated_stability} 条"
        )
        if not apply:
            print("这是dry-run，没有真的写入数据库，确认上面统计没问题后加 --apply 重新跑一遍")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--max-drift-meters", type=float, default=30.0,
        help="is_stable判定用的最大位移阈值(米)，要跟.env里AERIAL_FLIGHT_STABILITY_MAX_DRIFT_METERS一致，默认30",
    )
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理几条，不传就不限制")
    parser.add_argument(
        "--concurrency", type=int, default=32,
        help="同时读取SRT文件的并发数，NAS网络IO是瓶颈不是CPU，默认32，太慢可以调大，NAS扛不住就调小",
    )
    parser.add_argument("--apply", action="store_true", help="不加这个参数只是dry-run(读文件但不写库)，确认没问题后加这个才真的写入")
    args = parser.parse_args()
    asyncio.run(main(args.database_url, args.apply, args.max_drift_meters, args.limit, args.concurrency))
