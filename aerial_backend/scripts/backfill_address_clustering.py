"""一次性初始化脚本：给已入库的、storage_path符合会话命名规范(约5.2%)的
历史视频补city/site/flight_height/srt_height，并用complete-linkage层次
聚类(sklearn.cluster.AgglomerativeClustering, linkage='complete')把同一
个city下GPS+高度相近的视频归到同一个"地址"，写进locations表、回填
videos.site。

complete-linkage是"一次性/离线批量初始化"专用算法：distance_threshold
直接就是"簇内任意两点最大距离"的数学保证(合并判据本身就是两簇间最大
pairwise距离)，但它是全局重新评估的，不能在"已经分配好的地址不能再改"
的前提下用来处理后续新增视频——后续新视频走services/scanner.py里的
_assign_city_site(领导者算法，只新增不改动已有分配)。这两个算法的分工
是2026-10-29设计讨论敲定的，不要混用。

不符合命名规范/没有GPS坐标(collect_lon/lat为空)的视频不受影响，city/
site/flight_height/srt_height永久留空，这是预期行为，不是bug。

幂等/可续跑：按`site IS NULL`筛选候选行，已经成功分配过地址的不会重新
处理；没有GPS坐标的视频每次重跑都会被重新尝试一次(代价很小，几千条
量级无所谓)。

用法：
    uv run python aerial_backend/scripts/backfill_address_clustering.py \
        --database-url postgresql://user:pass@localhost:5432/aerial_db \
        --threshold-meters 20
    # 不加--apply是dry-run：会把每个city分出几个簇、每个簇的成员数、簇内
    # 最大两两距离都打印出来，先确认这些数字看起来合理，再加--apply正式写。
    # --threshold-meters要跟.env里AERIAL_ADDRESS_CLUSTER_MAX_DIAMETER_METERS
    # 保持一致(默认20)，这里单独传参是为了这个脚本不依赖app.config/.env，
    # 避免cwd不对导致.env读不到的问题(跟backfill_video_sha1.py同样的考虑)。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import asyncpg
import numpy as np
from sklearn.cluster import AgglomerativeClustering

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.geo import distance_3d_meters, latlon_to_local_meters  # noqa: E402
from app.services.session_naming import find_session_match  # noqa: E402
from app.services.video_metadata import extract_rel_alt_from_srt, find_srt_sidecar  # noqa: E402

PROGRESS_EVERY = 200


class VideoRow:
    __slots__ = ("sha256", "storage_path", "city", "raw_site", "flight_height", "srt_height", "lon", "lat")

    def __init__(self, sha256, storage_path, lon, lat):
        self.sha256 = sha256
        self.storage_path = storage_path
        self.lon = lon
        self.lat = lat
        self.city = None
        self.raw_site = None
        self.flight_height = None
        self.srt_height = None


def _height_of(row: VideoRow) -> float:
    return row.srt_height if row.srt_height is not None else float(row.flight_height)


def _cluster_one_city(city: str, rows: list[VideoRow], threshold_meters: float) -> list[list[VideoRow]]:
    """返回该city下的簇列表，每个簇是一个VideoRow列表。只有1个点的city
    不需要跑sklearn(没有pair可合并)，直接当成1个簇。"""
    if len(rows) == 1:
        return [rows]

    ref_lon, ref_lat = rows[0].lon, rows[0].lat
    coords = np.array(
        [[*latlon_to_local_meters(r.lon, r.lat, ref_lon, ref_lat), _height_of(r)] for r in rows]
    )
    model = AgglomerativeClustering(
        n_clusters=None, distance_threshold=threshold_meters, linkage="complete", metric="euclidean"
    )
    labels = model.fit_predict(coords)

    clusters: dict[int, list[VideoRow]] = defaultdict(list)
    for row, label in zip(rows, labels):
        clusters[label].append(row)
    return list(clusters.values())


def _max_pairwise_distance(members: list[VideoRow]) -> float:
    if len(members) < 2:
        return 0.0
    return max(
        distance_3d_meters(a.lon, a.lat, _height_of(a), b.lon, b.lat, _height_of(b))
        for a, b in combinations(members, 2)
    )


async def main(database_url: str, apply: bool, threshold_meters: float) -> None:
    conn = await asyncpg.connect(database_url, server_settings={"search_path": "aerial,public"})
    try:
        rows_raw = await conn.fetch(
            "SELECT sha256, storage_path, collect_lon, collect_lat FROM videos "
            "WHERE deleted_at IS NULL AND site IS NULL ORDER BY sha256"
        )
        print(f"待检查: {len(rows_raw)} 条(deleted_at IS NULL且site IS NULL的活记录)")

        matched = 0
        no_gps = 0
        by_city: dict[str, list[VideoRow]] = defaultdict(list)
        text_only_updates: list[VideoRow] = []  # 符合命名规范但没有GPS，只补文本字段

        for processed, r in enumerate(rows_raw, start=1):
            match = find_session_match(r["storage_path"])
            if match is None:
                continue
            matched += 1

            row = VideoRow(r["sha256"], r["storage_path"], r["collect_lon"], r["collect_lat"])
            row.city = match.group("city")
            row.raw_site = match.group("site")
            row.flight_height = int(match.group("height"))

            srt_path = find_srt_sidecar(row.storage_path)
            if srt_path is not None:
                row.srt_height = extract_rel_alt_from_srt(srt_path)

            if row.lon is None or row.lat is None:
                no_gps += 1
                text_only_updates.append(row)
            else:
                by_city[row.city].append(row)

            if processed % PROGRESS_EVERY == 0:
                print(f"[backfill_address] 扫描进度 {processed}/{len(rows_raw)}", flush=True)

        print(
            f"\n符合会话命名规范: {matched}/{len(rows_raw)}；"
            f"其中没有GPS坐标(不参与聚类，只补city/flight_height/srt_height): {no_gps}"
        )
        print(f"符合规范+有GPS、将参与聚类的city数: {len(by_city)}\n")

        # (city, raw_site前缀) -> 下一个可用序号；先用这个city下已有的locations
        # 占位，保证序号不跟locations表里已经存在的地址冲突(虽然site IS NULL
        # 筛选下通常不会有冲突，防御性处理)
        next_index: dict[tuple[str, str], int] = {}
        existing_locations = await conn.fetch("SELECT city, site FROM locations")
        for loc in existing_locations:
            for raw_site_guess in (loc["site"].rsplit("_", 1)[0],):
                suffix = loc["site"][len(raw_site_guess) + 1:]
                if suffix.isdigit():
                    key = (loc["city"], raw_site_guess)
                    next_index[key] = max(next_index.get(key, 0), int(suffix))

        total_clusters = 0
        location_inserts: list[tuple[str, str, float]] = []
        video_updates: list[tuple[bytes, str, str, int, float | None]] = []

        for city, rows in sorted(by_city.items()):
            clusters = _cluster_one_city(city, rows, threshold_meters)
            total_clusters += len(clusters)
            print(f"[{city}] {len(rows)}个视频 -> {len(clusters)}个地址")
            for members in clusters:
                raw_site_counts = Counter(m.raw_site for m in members)
                raw_site = raw_site_counts.most_common(1)[0][0]
                key = (city, raw_site)
                next_index[key] = next_index.get(key, 0) + 1
                site_label = f"{raw_site}_{next_index[key]}"
                max_dist = _max_pairwise_distance(members)
                print(
                    f"    {site_label}: {len(members)}个成员，簇内最大两两距离={max_dist:.2f}m"
                    + (" ⚠️ 超过阈值!" if max_dist > threshold_meters else "")
                )
                location_inserts.append((city, site_label, threshold_meters))
                for m in members:
                    video_updates.append((m.sha256, city, site_label, m.flight_height, m.srt_height))

        for row in text_only_updates:
            video_updates.append((row.sha256, row.city, None, row.flight_height, row.srt_height))

        print(f"\n共{total_clusters}个新地址，{len(video_updates)}条视频记录将被更新")

        if apply:
            async with conn.transaction():
                for city, site, thr in location_inserts:
                    await conn.execute(
                        "INSERT INTO locations (city, site, threshold_meters) VALUES ($1, $2, $3) "
                        "ON CONFLICT (city, site) DO NOTHING",
                        city, site, thr,
                    )
                for sha256, city, site, flight_height, srt_height in video_updates:
                    await conn.execute(
                        "UPDATE videos SET city = $2, site = $3, flight_height = $4, srt_height = $5 "
                        "WHERE sha256 = $1",
                        sha256, city, site, flight_height, srt_height,
                    )
            print("已写入数据库(--apply)")
        else:
            print("这是dry-run，没有真的写入数据库，确认上面输出没问题后加 --apply 重新跑一遍")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--threshold-meters", type=float, default=20.0,
        help="簇内最大两两距离(米)，要跟.env里AERIAL_ADDRESS_CLUSTER_MAX_DIAMETER_METERS一致，默认20",
    )
    parser.add_argument("--apply", action="store_true", help="不加这个参数只是dry-run，确认没问题后加这个才真的写入")
    args = parser.parse_args()
    asyncio.run(main(args.database_url, args.apply, args.threshold_meters))
