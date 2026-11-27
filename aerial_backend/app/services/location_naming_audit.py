"""
位置命名审核：从上传者输入的原始site名称（存储路径中解析），统计投票、
检测名称漂移(某location下的site文本不一致)和可能需合并的集群
(同一个site文本出现在多个location_id下)。

核心判断逻辑(tally_raw_site_votes/compute_votes_fingerprint/build_alert_candidates/
is_duplicate_fingerprint/is_already_grouped)是纯函数，不涉及DB访问，便于单独测试；
本文件末尾的compute_city_votes/detect_naming_alerts是薄的DB聚合与写入层，只负责
查数据、把结果接到上面这些纯函数上做判断、按判断结果插入alert行，不重新实现判断
逻辑本身。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Location, LocationNamingAlert, Video
from .session_naming import find_session_match


@dataclass
class AlertCandidate:
    """命名审核告警候选。

    `raw_site_votes`存完整票数(前端要展示"某个拼写被投了多少票")，但
    **票数不参与去重fingerprint**——fingerprint只看`fingerprint_input`，
    也就是"这个问题的身份"：
    - name_drift: 这个location下出现过的不同拼写的集合(sorted keys)
    - merge_candidate: (撞名的site文本, 涉及的location_id集合)

    为什么必须分开(终审Critical#1)：原来fingerprint直接哈希整个
    {raw_site: 次数}字典，结果这个city每进一个新视频票数就变一次，
    fingerprint跟着变，去重失效，同一个命名问题被反复插成新的pending
    告警；而raw site文本存在storage_path里(按本方案约束永不改写)，
    所以拼写漂移这种条件是**永久性**的，一旦存在就会每来一个视频复活
    一条新告警，哪怕人工早就confirm/dismiss过了。改成只哈希身份后：
    票数变化不再重复告警，真正出现第三种拼写/有新location加入同名
    家族时才会重新告警。
    """
    alert_type: Literal["name_drift", "merge_candidate"]
    city: str
    location_ids: list[int]
    raw_site_votes: dict
    suggested_value: str
    fingerprint_input: list


def tally_raw_site_votes(video_rows: list[tuple[int, str]]) -> dict[str, dict[int, dict[str, int]]]:
    """
    从(location_id, storage_path)列表统计各城市各location下上传者输入的原始site名称投票。

    返回结构：{city: {location_id: {raw_site: 次数}}}
    """
    result: dict[str, dict[int, dict[str, int]]] = {}

    for location_id, storage_path in video_rows:
        match = find_session_match(storage_path)
        if not match:
            # 解析不出来的路径直接跳过
            continue

        city = match.group("city")
        site = match.group("site")

        # 确保city层级存在
        if city not in result:
            result[city] = {}

        # 确保location_id层级存在
        if location_id not in result[city]:
            result[city][location_id] = {}

        # 递增site的投票数
        if site not in result[city][location_id]:
            result[city][location_id][site] = 0
        result[city][location_id][site] += 1

    return result


def compute_votes_fingerprint(fingerprint_input) -> str:
    """
    对"问题身份"(AlertCandidate.fingerprint_input)计算fingerprint。

    按key排序后json序列化，再取md5，保证同样内容无论dict插入顺序如何都
    产生同一个fingerprint。

    注意输入**不是**{raw_site: 次数}票数字典(见AlertCandidate的docstring)：
    票数只用于展示，让它流进哈希会导致每进一个新视频就重复告警一次。
    """
    serialized = json.dumps(fingerprint_input, sort_keys=True)
    return hashlib.md5(serialized.encode()).hexdigest()


def build_alert_candidates(city: str, city_votes: dict[int, dict[str, int]]) -> list[AlertCandidate]:
    """
    从单个城市的投票数据({location_id: {raw_site: 次数}})生成告警候选。

    参数：
    - city: 城市名称（会被记录到每个AlertCandidate）
    - city_votes: 该城市的投票数据

    检测两类问题：
    1. name_drift: 某location_id下的site文本不一致(超过1个不同的key)
    2. merge_candidate: 某个site文本出现在超过1个location_id
    """
    candidates: list[AlertCandidate] = []

    # 检测 name_drift：某location_id下有多个不同的site文本
    for location_id, site_votes in city_votes.items():
        if len(site_votes) > 1:  # 多个不同的site
            # 建议值是投票数最多的site
            suggested_value = max(site_votes.items(), key=lambda x: x[1])[0]
            candidates.append(AlertCandidate(
                alert_type="name_drift",
                city=city,
                location_ids=[location_id],
                raw_site_votes=site_votes,
                suggested_value=suggested_value,
                # 身份 = 这个location下出现过的不同拼写的集合，不含票数
                fingerprint_input=sorted(site_votes.keys()),
            ))

    # 检测 merge_candidate：某个site文本出现在多个location_id
    # 先构建反向索引：site -> [location_ids]
    site_to_locations: dict[str, list[int]] = {}
    for location_id, site_votes in city_votes.items():
        for site in site_votes.keys():
            if site not in site_to_locations:
                site_to_locations[site] = []
            site_to_locations[site].append(location_id)

    # 对于出现在多个location_id的site，生成merge_candidate
    # 用(raw_site_text, frozenset(location_ids))作为key，避免同location_ids下不同site文本的merge_candidate被重复丢弃
    seen_merge_candidates: set[tuple[str, frozenset[int]]] = set()
    for site, location_ids in site_to_locations.items():
        if len(location_ids) > 1:
            # 避免重复（但要允许不同site文本出现在相同location_ids的情况）
            merge_key = (site, frozenset(location_ids))
            if merge_key not in seen_merge_candidates:
                seen_merge_candidates.add(merge_key)

                # raw_site_votes格式：{raw_site: {location_id: count}}
                raw_site_votes_nested: dict[str, dict[int, int]] = {}
                for loc_id in location_ids:
                    if site not in raw_site_votes_nested:
                        raw_site_votes_nested[site] = {}
                    raw_site_votes_nested[site][loc_id] = city_votes[loc_id][site]

                candidates.append(AlertCandidate(
                    alert_type="merge_candidate",
                    city=city,
                    location_ids=sorted(location_ids),
                    raw_site_votes=raw_site_votes_nested,
                    suggested_value=site,
                    # 身份 = (撞名的site文本, 涉及的location_id集合)，不含票数
                    fingerprint_input=[site, sorted(location_ids)],
                ))

    return candidates


def is_duplicate_fingerprint(fingerprint: str, existing_fingerprints: set[str]) -> bool:
    """
    判断fingerprint是否已存在于existing_fingerprints中。

    去重不看status——旧设计只挡pending会导致confirm/dismiss过的提示
    在下次扫描时被原样复活。
    """
    return fingerprint in existing_fingerprints


def is_already_grouped(location_ids: list[int], group_id_by_location: dict[int, int | None]) -> bool:
    """
    判断这些location_id是否已经分组到同一个group。

    需要满足：
    1. 所有location_id都有非空group_id
    2. 所有location_id的group_id相同
    """
    if not location_ids:
        return False

    # 获取第一个location的group_id作为参考
    first_group_id = group_id_by_location.get(location_ids[0])

    # 如果第一个是None，不满足条件
    if first_group_id is None:
        return False

    # 检查所有location_id是否都有相同的非None group_id
    for location_id in location_ids:
        if group_id_by_location.get(location_id) != first_group_id:
            return False

    return True


async def tally_all_votes(session: AsyncSession) -> dict[str, dict[int, dict[str, int]]]:
    """
    查location_id不为空的全部(活着的)视频的(location_id, storage_path)，
    喂给tally_raw_site_votes统计投票，返回{city: {location_id: {site: 次数}}}。

    **不按Video.city列过滤**(终审Important#3)：本方案的硬约束是city/site
    一律用find_session_match从storage_path重新解析，不读存着的名字列当
    事实来源——Video.city是扫描时写进去的快照，会过期(见
    services/session_naming.py的docstring里2026-11-03设备白名单变更
    那个真实案例)。这里的city分组完全来自重新解析的结果。

    deleted_at不为空的视频(被瘦身下线的旧记录)不参与统计——它们的storage_path
    已经不代表"这次扫描时文件实际在哪"，混进来会污染投票。
    """
    rows_result = await session.execute(
        select(Video.location_id, Video.storage_path)
        .where(Video.location_id.isnot(None))
        .where(Video.deleted_at.is_(None))
    )
    video_rows = [(location_id, storage_path) for location_id, storage_path in rows_result]
    return tally_raw_site_votes(video_rows)


async def list_cities_from_videos(session: AsyncSession) -> set[str]:
    """从重新解析storage_path得到的投票数据里取city列表(不是
    `select distinct Location.city`——同上，存着的名字列不是事实来源)。
    供backfill脚本决定"要扫哪些city"。"""
    return set(await tally_all_votes(session))


async def compute_city_votes(session: AsyncSession, city: str) -> dict[int, dict[str, int]]:
    """取单个city那一层的投票数据。city参数只用来从"全量重新解析"的结果里
    挑一个key出来，不参与SQL过滤(见tally_all_votes)。"""
    return (await tally_all_votes(session)).get(city, {})


async def detect_naming_alerts(session: AsyncSession, city: str) -> int:
    """
    扫描单个city，把build_alert_candidates算出来的候选落成location_naming_alerts
    表里的pending记录。只是薄的DB聚合+写入层：真正"算出候选""要不要跳过"的判断
    全部交给Task1的纯函数(build_alert_candidates/is_already_grouped/
    is_duplicate_fingerprint)，这里不重新实现判断逻辑。

    返回这次调用里真正新插入的条数(供backfill脚本打印统计用)——不是候选总数：
    候选里被"已经分组"或"fingerprint已存在"挡掉的不算，被并发扫描抢先插入
    (IntegrityError)的也不算。

    不在这里commit——调用方(scanner.py钩子/backfill脚本)决定何时commit，这样
    这次扫描新插入的alert行能跟同一次扫描的其他改动(比如新video/新location)
    一起成功或一起回滚。
    """
    city_votes = await compute_city_votes(session, city)
    candidates = build_alert_candidates(city, city_votes)

    inserted_count = 0
    for candidate in candidates:
        # location_ids按升序排好再存/比较，保证同一组合不会因为顺序不同绕过
        # 下面的唯一索引去重，也不会因为顺序不同查不到已有的fingerprint
        sorted_location_ids = sorted(candidate.location_ids)
        # 只哈希"问题身份"，不哈希票数(见AlertCandidate的docstring)
        fingerprint = compute_votes_fingerprint(candidate.fingerprint_input)

        if candidate.alert_type == "merge_candidate":
            group_rows_result = await session.execute(
                select(Location.id, Location.location_group_id)
                .where(Location.id.in_(sorted_location_ids))
            )
            group_id_by_location = {
                location_id: group_id for location_id, group_id in group_rows_result
            }
            if is_already_grouped(sorted_location_ids, group_id_by_location):
                # 这些location已经被人工确认是同一个地址了，不用再提示合并
                continue

        existing_fingerprints_result = await session.execute(
            select(LocationNamingAlert.votes_fingerprint)
            .where(LocationNamingAlert.alert_type == candidate.alert_type)
            .where(LocationNamingAlert.location_ids == sorted_location_ids)
        )
        existing_fingerprints = {fp for (fp,) in existing_fingerprints_result}
        if is_duplicate_fingerprint(fingerprint, existing_fingerprints):
            # 去重不看status(见is_duplicate_fingerprint的docstring)：这个组合的
            # 问题身份(拼写集合/撞名site+location集合，不含票数)之前已经提示过
            # (不管后来是confirm还是dismiss)，不重复插入
            continue

        alert = LocationNamingAlert(
            alert_type=candidate.alert_type,
            city=candidate.city,
            location_ids=sorted_location_ids,
            raw_site_votes=candidate.raw_site_votes,
            votes_fingerprint=fingerprint,
            suggested_value=candidate.suggested_value,
        )
        try:
            # 跟scanner.py:_apply_session_metadata里同样的SAVEPOINT套路：
            # 定时扫描和backfill脚本可能并发对同一个city跑这个函数，两边都在
            # 上面的fingerprint查询里没查到之后抢着插入同一个组合，后提交的
            # 那边会撞idx_location_naming_alerts_dedupe唯一索引冲突。只回滚到
            # SAVEPOINT，把这次冲突当成"已经有人插过了"处理，不让整个调用方的
            # 事务(可能还夹着这次扫描的其他改动)被拖垮
            async with session.begin_nested():
                session.add(alert)
                await session.flush()
        except IntegrityError:
            continue
        else:
            inserted_count += 1

    return inserted_count
