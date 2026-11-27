"""
位置命名审核：从上传者输入的原始site名称（存储路径中解析），统计投票、
检测名称漂移(某location下的site文本不一致)和可能需合并的集群
(同一个site文本出现在多个location_id下)。

纯函数模块，不涉及DB访问，便于单独测试。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from .session_naming import find_session_match


@dataclass
class AlertCandidate:
    """命名审核告警候选。"""
    alert_type: Literal["name_drift", "merge_candidate"]
    city: str
    location_ids: list[int]
    raw_site_votes: dict
    suggested_value: str


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


def compute_votes_fingerprint(raw_site_votes: dict[str, int]) -> str:
    """
    对单个location_id的{raw_site: 次数}字典计算fingerprint。

    按key排序后json序列化，再取md5，保证同样内容无论插入顺序如何都产生
    同一个fingerprint。
    """
    serialized = json.dumps(raw_site_votes, sort_keys=True)
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
