"""一次性初始化脚本：给已入库的、storage_path符合会话命名规范(约5.2%)的
历史视频补city/site/flight_height/srt_height，并用complete-linkage层次
聚类(sklearn.cluster.AgglomerativeClustering, linkage='complete')把同一
个city下GPS+高度相近的视频归到同一个"地址"，写进locations表、回填
videos.site。

同时会重新解析一遍这批视频的collect_lon/collect_lat(不是直接用DB里已经
存的旧值)：2026-10-29发现起飞爬升阶段录的SRT，开头几条GPS/高度读数可能
还在起飞点附近，不是真正的拍摄位置，extract_gps_from_srt/
extract_rel_alt_from_srt已经改成取全程中位数而不是第一条，但DB里已经
入库的旧collect_lon/collect_lat还是当年用"第一条"算出来的——这个脚本
既然要用坐标做聚类，顺手重新解析一遍、把更准的值也写回videos表。sidecar
SRT文件如果已经找不到了(比如被挪动/清理过)，退回用DB里的旧值，不会把
原本有效的坐标清空成NULL。

这个脚本只重新解析"符合会话命名规范"的这1245条，不是全量23844条——
如果想给全部视频(不限于符合命名规范的)刷新collect_lon/collect_lat，
用已有的全量回填入口：`uv run main.py --backfill --force`
(调用的是app.services.backfill.backfill_video_metadata(only_missing=False))。

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
from app.services.geo import distance_3d_meters, latlon_to_local_meters, trajectory_diameter_meters  # noqa: E402
from app.services.session_naming import find_session_match  # noqa: E402
from app.services.video_metadata import (  # noqa: E402
    extract_gps_from_srt, extract_rel_alt_from_srt, extract_srt_trajectory, find_srt_sidecar,
)

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


def _nearest_other(row: VideoRow, all_rows: list[VideoRow]) -> tuple[VideoRow, float] | None:
    """在同city的全部候选点里(不分簇)，找离row最近的另一个点及距离——用于
    诊断"只有1个成员的簇"到底是真的物理上孤立，还是卡在阈值边缘/数据有
    问题(比如height来源对不上导致算出来的距离被人为拉大)。"""
    best = None
    for other in all_rows:
        if other is row:
            continue
        d = distance_3d_meters(row.lon, row.lat, _height_of(row), other.lon, other.lat, _height_of(other))
        if best is None or d < best[1]:
            best = (other, d)
    return best


def _diagnose_small_cluster(members: list[VideoRow], all_rows: list[VideoRow], threshold_meters: float) -> None:
    """簇成员数<=2时，打印每个成员的原始坐标/高度来源，以及它在整个city里
    (不限于本簇)最近的邻居是谁、距离多少——方便判断是真孤立还是height
    来源(srt_height缺失退回flight_height)把距离人为拉大了。"""
    member_ids = {id(m) for m in members}
    for m in members:
        height_source = "srt_height" if m.srt_height is not None else "flight_height(回退)"
        print(
            f"        {m.storage_path}\n"
            f"          lon={m.lon:.6f} lat={m.lat:.6f} "
            f"height={_height_of(m):.1f}({height_source}) "
            f"srt_height={m.srt_height} flight_height={m.flight_height}"
        )
        # 轨迹诊断：区分"整段视频全程都在这个高度(可能是一次降落/起飞段的
        # 独立文件，比如DJI按时长自动切片，最后一段整段都在降落)"还是
        # "视频内部本身就从高到低/低到高有个过渡"——srt_height中位数已经
        # 滤掉了开头/结尾的瞬态，如果这里diameter还是很大，说明不是瞬态，
        # 是这条视频本身大半程都在明显移动/变高度
        srt_path = find_srt_sidecar(m.storage_path)
        trajectory = extract_srt_trajectory(srt_path) if srt_path is not None else None
        if trajectory is not None and len(trajectory) >= 2:
            heights = [h for _, _, h in trajectory]
            diameter = trajectory_diameter_meters(trajectory)
            print(
                f"          自身轨迹: {len(trajectory)}帧，高度范围[{min(heights):.1f}, {max(heights):.1f}]，"
                f"轨迹内最大位移={diameter:.2f}m"
                + (" ⚠️ 视频内部本身就有明显移动/高度变化" if diameter > 30 else "(内部基本稳定)")
            )
        nearest = _nearest_other(m, all_rows)
        if nearest is None:
            continue
        other, dist = nearest
        other_source = "srt_height" if other.srt_height is not None else "flight_height(回退)"
        # 最近邻如果不在本簇里、但距离又在阈值内，说明complete-linkage没把
        # 它俩合并是因为链式约束(跟簇里别的成员距离超了)，不是算法bug，但
        # 值得人工看一眼
        flag = (
            " ⚠️ 在阈值内但不在同一簇(可能是跟簇内其他成员的链式约束导致，建议人工确认)"
            if dist <= threshold_meters and id(other) not in member_ids
            else ""
        )
        print(
            f"          最近邻: {other.storage_path} 距离={dist:.2f}m{flag}\n"
            f"          最近邻height={_height_of(other):.1f}({other_source}) "
            f"srt_height={other.srt_height} flight_height={other.flight_height}"
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

            # collect_lon/collect_lat在DB里是NUMERIC，asyncpg会返回
            # decimal.Decimal而不是float——这批行里有些会在下面被重新解析
            # 的SRT坐标(float)覆盖，有些(sidecar找不到)保留这里的DB旧值，
            # 两种类型混在同一个rows列表里做距离运算会直接报TypeError，
            # 这里统一转成float规避
            db_lon = float(r["collect_lon"]) if r["collect_lon"] is not None else None
            db_lat = float(r["collect_lat"]) if r["collect_lat"] is not None else None
            row = VideoRow(r["sha256"], r["storage_path"], db_lon, db_lat)
            row.city = match.group("city")
            row.raw_site = match.group("site")
            row.flight_height = int(match.group("height"))

            srt_path = find_srt_sidecar(row.storage_path)
            if srt_path is not None:
                # 用新的"全程中位数"逻辑重新解析一遍，比DB里当年用"第一条"
                # 算出来的旧collect_lon/collect_lat更准；sidecar解析不出来
                # (或者文件已经不在了)就保留上面从DB读到的旧值，不清空
                gps = extract_gps_from_srt(srt_path)
                if gps is not None:
                    row.lon, row.lat = gps
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
        video_updates: list[tuple[bytes, str, str | None, int, float | None, float | None, float | None]] = []

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
                if len(members) <= 2:
                    _diagnose_small_cluster(members, rows, threshold_meters)
                location_inserts.append((city, site_label, threshold_meters))
                for m in members:
                    video_updates.append(
                        (m.sha256, city, site_label, m.flight_height, m.srt_height, m.lon, m.lat)
                    )

        for row in text_only_updates:
            video_updates.append(
                (row.sha256, row.city, None, row.flight_height, row.srt_height, row.lon, row.lat)
            )

        print(f"\n共{total_clusters}个新地址，{len(video_updates)}条视频记录将被更新")

        if apply:
            async with conn.transaction():
                for city, site, thr in location_inserts:
                    await conn.execute(
                        "INSERT INTO locations (city, site, threshold_meters) VALUES ($1, $2, $3) "
                        "ON CONFLICT (city, site) DO NOTHING",
                        city, site, thr,
                    )
                for sha256, city, site, flight_height, srt_height, lon, lat in video_updates:
                    await conn.execute(
                        "UPDATE videos SET city = $2, site = $3, flight_height = $4, srt_height = $5, "
                        "collect_lon = $6, collect_lat = $7 WHERE sha256 = $1",
                        sha256, city, site, flight_height, srt_height, lon, lat,
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
