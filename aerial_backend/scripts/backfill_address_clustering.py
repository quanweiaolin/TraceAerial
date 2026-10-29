"""一次性初始化脚本：给已入库的、storage_path符合会话命名规范(约5.2%)的
历史视频补city/site/flight_height/srt_height，并用complete-linkage层次
聚类(sklearn.cluster.AgglomerativeClustering, linkage='complete')把同一
个city下GPS+高度相近的视频归到同一个"地址"，写进locations表、回填
videos.site。

2026-10-30起改成直接读DB里的collect_lon/collect_lat/srt_height/is_stable，
不再自己重新解析SRT——这4个字段现在由scripts/refresh_srt_fields.py统一
维护(同一套"取全程中位数"的提取逻辑，见app.services.video_metadata)，
这个脚本只管拿现成的值去聚类，不重复一份解析逻辑。

**前置条件**：必须先跑过一遍`refresh_srt_fields.py --apply`，让这几个
字段反映最新逻辑算出来的值，才能跑这个脚本——如果DB里还是老值(比如还没
跑过refresh_srt_fields.py，或者是很久以前"取第一条"算出来的旧值)，这里
聚类用的就是不准的坐标/高度，结果同样不准。这个脚本本身不会提醒你有没有
按顺序做，需要人工保证。

complete-linkage是"一次性/离线批量初始化"专用算法：distance_threshold
直接就是"簇内任意两点最大距离"的数学保证(合并判据本身就是两簇间最大
pairwise距离)，但它是全局重新评估的，不能在"已经分配好的地址不能再改"
的前提下用来处理后续新增视频——后续新视频走services/scanner.py里的
_assign_city_site(领导者算法，只新增不改动已有分配)。这两个算法的分工
是2026-10-29设计讨论敲定的，不要混用。

不符合命名规范/没有GPS坐标(collect_lon/lat为空)的视频不受影响，city/
site/flight_height/srt_height永久留空，这是预期行为，不是bug。

2026-10-29生产端实跑发现：有些"单成员地址"的height中位数远低于标称值
(比如3.2m vs标称80m)，排查发现是这条视频自己的SRT轨迹本身就从起飞点
一路爬升到巡航高度甚至更高(比如这次真实案例：4795帧，高度范围从-2.5m
到101.3m)，median只是把"整段飞行"的中间值取出来，不代表任何有意义的
单点位置——这种视频不该参与聚类(不该用一个不代表真实拍摄位置的中位数
点去建一个新地址)。用DB里的is_stable过滤：is_stable为False的视频不
参与聚类，跟没有GPS坐标一样只补city/flight_height这几个字段(srt_height/
collect_lon/collect_lat/is_stable已经是refresh_srt_fields.py写好的，
这里不用再写)，site留空。is_stable是None(没有可用轨迹，比如老格式SRT)
的视频仍然正常参与聚类——没有证据说它不稳定，不能当成"不稳定"处理。
诊断小簇时(_diagnose_small_cluster)会按需重新读一下SRT算轨迹细节(高度
范围/内部最大位移)用于人工排查展示，不影响聚类决策本身(决策只认DB里
的is_stable)。

幂等/可续跑：按`site IS NULL`筛选候选行，已经成功分配过地址的不会重新
处理；没有GPS坐标/is_stable=False被排除在聚类外的视频，site一直是NULL，
每次重跑都会被重新尝试一次(代价很小，几千条量级无所谓；如果SRT本身没变，
重跑结果也不会变，不是bug)。

用法：
    uv run python aerial_backend/scripts/backfill_address_clustering.py \
        --database-url postgresql://user:pass@localhost:5432/aerial_db \
        --threshold-meters 20 --max-drift-meters 30
    # 不加--apply是dry-run：会把每个city分出几个簇、每个簇的成员数、簇内
    # 最大两两距离都打印出来，先确认这些数字看起来合理，再加--apply正式写。
    # --threshold-meters要跟.env里AERIAL_ADDRESS_CLUSTER_MAX_DIAMETER_METERS、
    # --max-drift-meters要跟AERIAL_FLIGHT_STABILITY_MAX_DRIFT_METERS保持
    # 一致(默认分别是20/30)，这里单独传参是为了这个脚本不依赖app.config/
    # .env，避免cwd不对导致.env读不到的问题(跟backfill_video_sha1.py同样
    # 的考虑)。
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
from app.services.video_metadata import extract_srt_trajectory, find_srt_sidecar  # noqa: E402

PROGRESS_EVERY = 200


class VideoRow:
    __slots__ = (
        "sha256", "storage_path", "city", "raw_site", "flight_height", "srt_height", "lon", "lat", "is_stable",
    )

    def __init__(self, sha256, storage_path, lon, lat, srt_height, is_stable):
        self.sha256 = sha256
        self.storage_path = storage_path
        self.lon = lon
        self.lat = lat
        self.srt_height = srt_height
        self.is_stable = is_stable
        self.city = None
        self.raw_site = None
        self.flight_height = None


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


def _diagnose_small_cluster(
    members: list[VideoRow], all_rows: list[VideoRow], threshold_meters: float, max_drift_meters: float
) -> None:
    """簇成员数<=2时，打印每个成员的原始坐标/高度来源，以及它在整个city里
    (不限于本簇)最近的邻居是谁、距离多少——方便判断是真孤立还是height
    来源(srt_height缺失退回flight_height)把距离人为拉大了。只有少数小簇
    会走到这里，按需重新读一下SRT打印轨迹细节(高度范围/内部最大位移)方便
    人工排查，不影响聚类决策本身——决策已经直接用DB里现成的is_stable，
    这里的重新解析纯粹是展示用途。"""
    member_ids = {id(m) for m in members}
    for m in members:
        height_source = "srt_height" if m.srt_height is not None else "flight_height(回退)"
        print(
            f"        {m.storage_path}\n"
            f"          lon={m.lon:.6f} lat={m.lat:.6f} "
            f"height={_height_of(m):.1f}({height_source}) "
            f"srt_height={m.srt_height} flight_height={m.flight_height} is_stable={m.is_stable}"
        )
        srt_path = find_srt_sidecar(m.storage_path)
        trajectory = extract_srt_trajectory(srt_path) if srt_path is not None else None
        if trajectory is not None and len(trajectory) >= 2:
            heights = [h for _, _, h in trajectory]
            diameter = trajectory_diameter_meters(trajectory)
            print(
                f"          自身轨迹(重新解析，仅展示): 高度范围[{min(heights):.1f}, {max(heights):.1f}]，"
                f"轨迹内最大位移={diameter:.2f}m"
                + (" ⚠️ 视频内部本身就有明显移动/高度变化" if diameter > max_drift_meters else "(内部基本稳定)")
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


async def main(database_url: str, apply: bool, threshold_meters: float, max_drift_meters: float) -> None:
    conn = await asyncpg.connect(database_url, server_settings={"search_path": "aerial,public"})
    try:
        rows_raw = await conn.fetch(
            "SELECT sha256, storage_path, collect_lon, collect_lat, srt_height, is_stable FROM videos "
            "WHERE deleted_at IS NULL AND site IS NULL ORDER BY sha256"
        )
        print(f"待检查: {len(rows_raw)} 条(deleted_at IS NULL且site IS NULL的活记录)")

        matched = 0
        no_gps = 0
        unstable = 0
        by_city: dict[str, list[VideoRow]] = defaultdict(list)
        # 符合命名规范但没有GPS，或者有GPS但自身轨迹不稳定(is_stable=False)——
        # 两种都不参与聚类，只补文本字段+已经重新解析出来的collect_lon/lat/
        # srt_height/is_stable
        excluded_from_clustering: list[VideoRow] = []

        for processed, r in enumerate(rows_raw, start=1):
            match = find_session_match(r["storage_path"])
            if match is None:
                continue
            matched += 1

            # collect_lon/collect_lat在DB里是NUMERIC，asyncpg会返回
            # decimal.Decimal而不是float，跟后面distance_3d_meters/
            # latlon_to_local_meters里的float运算混用会报TypeError，
            # 这里统一转成float规避(srt_height是DOUBLE PRECISION，
            # asyncpg本来就返回float，不用转)
            db_lon = float(r["collect_lon"]) if r["collect_lon"] is not None else None
            db_lat = float(r["collect_lat"]) if r["collect_lat"] is not None else None
            row = VideoRow(r["sha256"], r["storage_path"], db_lon, db_lat, r["srt_height"], r["is_stable"])
            row.city = match.group("city")
            row.raw_site = match.group("site")
            row.flight_height = int(match.group("height"))

            if row.lon is None or row.lat is None:
                no_gps += 1
                excluded_from_clustering.append(row)
            elif row.is_stable is False:
                unstable += 1
                excluded_from_clustering.append(row)
            else:
                by_city[row.city].append(row)

            if processed % PROGRESS_EVERY == 0:
                print(f"[backfill_address] 扫描进度 {processed}/{len(rows_raw)}", flush=True)

        print(
            f"\n符合会话命名规范: {matched}/{len(rows_raw)}；"
            f"其中没有GPS坐标: {no_gps}，自身轨迹不稳定(is_stable=False): {unstable}"
            f"(这两类都不参与聚类，只补city/flight_height/srt_height/collect_lon/lat/is_stable)"
        )
        print(f"符合规范+有GPS+稳定、将参与聚类的city数: {len(by_city)}\n")

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
        # 只需要写city/site/flight_height：collect_lon/collect_lat/srt_height/
        # is_stable是从DB读出来的现成值，这个脚本没有改动过它们，不用写回去
        video_updates: list[tuple[bytes, str, str | None, int]] = []

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
                    _diagnose_small_cluster(members, rows, threshold_meters, max_drift_meters)
                location_inserts.append((city, site_label, threshold_meters))
                for m in members:
                    video_updates.append((m.sha256, city, site_label, m.flight_height))

        for row in excluded_from_clustering:
            video_updates.append((row.sha256, row.city, None, row.flight_height))

        print(f"\n共{total_clusters}个新地址，{len(video_updates)}条视频记录将被更新")

        if apply:
            async with conn.transaction():
                for city, site, thr in location_inserts:
                    await conn.execute(
                        "INSERT INTO locations (city, site, threshold_meters) VALUES ($1, $2, $3) "
                        "ON CONFLICT (city, site) DO NOTHING",
                        city, site, thr,
                    )
                for sha256, city, site, flight_height in video_updates:
                    await conn.execute(
                        "UPDATE videos SET city = $2, site = $3, flight_height = $4 WHERE sha256 = $1",
                        sha256, city, site, flight_height,
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
    parser.add_argument(
        "--max-drift-meters", type=float, default=30.0,
        help="is_stable判定用的最大位移阈值(米)，要跟.env里AERIAL_FLIGHT_STABILITY_MAX_DRIFT_METERS一致，默认30",
    )
    parser.add_argument("--apply", action="store_true", help="不加这个参数只是dry-run，确认没问题后加这个才真的写入")
    args = parser.parse_args()
    asyncio.run(main(args.database_url, args.apply, args.threshold_meters, args.max_drift_meters))
