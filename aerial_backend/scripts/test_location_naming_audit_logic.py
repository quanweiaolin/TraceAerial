import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.location_naming_audit import (
    tally_raw_site_votes, compute_votes_fingerprint, build_alert_candidates,
    is_duplicate_fingerprint, is_already_grouped,
)


def test_tally_groups_by_reparsed_city_and_site():
    rows = [
        (22, "/mnt/nas/.../dongguan_guanfogaosuchuzadao_m3e_300m_lixiupeng_20230217/DJI_.../a.MP4"),
        (23, "/mnt/nas/.../dongguan_guanfogaosuchuzadao_m3e_300m_lixiupeng_20230217/DJI_.../b.MP4"),
        (99, "/mnt/nas/.../不符合命名规范的历史路径/c.MP4"),  # 解析不出来，应跳过
    ]
    result = tally_raw_site_votes(rows)
    assert result == {"dongguan": {22: {"guanfogaosuchuzadao": 1}, 23: {"guanfogaosuchuzadao": 1}}}


def test_tally_keeps_different_cities_separate_even_with_same_site_text():
    rows = [
        (22, "/mnt/nas/.../dongguan_guanfogaosuchuzadao_m3e_300m_lixiupeng_20230217/DJI_.../a.MP4"),
        (50, "/mnt/nas/.../changchun_guanfogaosuchuzadao_m3e_200m_wangziyu_20260912/DJI_.../a.MP4"),
    ]
    result = tally_raw_site_votes(rows)
    # 两个city各自只看到自己的一个location_id，互不污染——即使site文本撞了
    assert result == {
        "dongguan": {22: {"guanfogaosuchuzadao": 1}},
        "changchun": {50: {"guanfogaosuchuzadao": 1}},
    }


def test_duplicate_fingerprint_detected_regardless_of_existing_status():
    # 去重不看status——这是builder-check发现的原漏洞：旧设计只挡pending，
    # 导致confirm/dismiss过的提示在下次扫描(投票数据没变)时被原样复活
    assert is_duplicate_fingerprint("abc123", {"abc123"}) is True
    assert is_duplicate_fingerprint("abc123", set()) is False


def test_merge_candidate_suppressed_when_already_grouped():
    assert is_already_grouped([22, 23], {22: 5, 23: 5}) is True
    assert is_already_grouped([22, 23], {22: 5, 23: None}) is False
    assert is_already_grouped([22, 23], {22: 5, 23: 6}) is False


def test_clean_cluster_no_alert():
    candidates = build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 50}})
    assert candidates == []


def test_single_video_location_no_drift_alert():
    candidates = build_alert_candidates("dongguan", {23: {"guanfogaosuchuzadao": 1}})
    assert candidates == []


def test_name_drift_suggests_majority():
    candidates = build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 48, "guanfugaosuchuzadao": 2}})
    assert len(candidates) == 1
    assert candidates[0].alert_type == "name_drift"
    assert candidates[0].suggested_value == "guanfogaosuchuzadao"
    assert candidates[0].city == "dongguan"  # 验证city字段被正确设置


def test_merge_candidate_across_location_ids():
    candidates = build_alert_candidates("dongguan", {
        22: {"guanfogaosuchuzadao": 50},
        23: {"guanfogaosuchuzadao": 1},
    })
    assert len(candidates) == 1
    assert candidates[0].alert_type == "merge_candidate"
    assert set(candidates[0].location_ids) == {22, 23}
    assert candidates[0].city == "dongguan"  # 验证city字段被正确设置


def test_merge_candidate_different_sites_same_locations():
    # 测试Important #2：两个不同的site文本都映射到相同的location_ids，
    # 应该生成2个独立的merge_candidate，而不是被去重时丢弃
    candidates = build_alert_candidates("dongguan", {
        22: {"a": 1, "b": 1},
        23: {"a": 1, "b": 1},
    })

    # 筛选出只是merge_candidate类型的候选（排除name_drift）
    merge_candidates = [c for c in candidates if c.alert_type == "merge_candidate"]
    assert len(merge_candidates) == 2, f"Expected 2 merge_candidates, got {len(merge_candidates)}"

    # 两个merge_candidate应该对应两个不同的site
    suggested_values = {c.suggested_value for c in merge_candidates}
    assert suggested_values == {"a", "b"}, f"Expected {{'a', 'b'}}, got {suggested_values}"

    # 两个merge_candidate都应该涉及相同的location_ids
    for c in merge_candidates:
        assert c.alert_type == "merge_candidate"
        assert set(c.location_ids) == {22, 23}
        assert c.city == "dongguan"


def test_fingerprint_stable_regardless_of_key_order():
    a = compute_votes_fingerprint({"guanfogaosuchuzadao": 48, "guanfugaosuchuzadao": 2})
    b = compute_votes_fingerprint({"guanfugaosuchuzadao": 2, "guanfogaosuchuzadao": 48})
    assert a == b


def _fingerprint_of_only(candidates, alert_type):
    picked = [c for c in candidates if c.alert_type == alert_type]
    assert len(picked) == 1, f"Expected exactly 1 {alert_type}, got {len(picked)}"
    return compute_votes_fingerprint(picked[0].fingerprint_input)


def test_name_drift_fingerprint_ignores_vote_counts():
    # 终审Critical#1：票数变了(这个city又进了几个新视频)不算新问题，
    # fingerprint必须不变，否则同一个拼写漂移会被反复提示、
    # confirm/dismiss过的也会永远复活
    a = _fingerprint_of_only(
        build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 48, "guanfugaosuchuzadao": 2}}),
        "name_drift",
    )
    b = _fingerprint_of_only(
        build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 300, "guanfugaosuchuzadao": 7}}),
        "name_drift",
    )
    assert a == b


def test_name_drift_fingerprint_changes_when_new_spelling_appears():
    # 真的多出第三种拼写 = 新问题，应该重新提示
    a = _fingerprint_of_only(
        build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 48, "guanfugaosuchuzadao": 2}}),
        "name_drift",
    )
    c = _fingerprint_of_only(
        build_alert_candidates(
            "dongguan",
            {22: {"guanfogaosuchuzadao": 48, "guanfugaosuchuzadao": 2, "guanfogaosuchuzhadao": 1}},
        ),
        "name_drift",
    )
    assert a != c


def test_merge_candidate_fingerprint_ignores_vote_counts():
    a = _fingerprint_of_only(
        build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 50}, 23: {"guanfogaosuchuzadao": 1}}),
        "merge_candidate",
    )
    b = _fingerprint_of_only(
        build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 51}, 23: {"guanfogaosuchuzadao": 9}}),
        "merge_candidate",
    )
    assert a == b


def test_merge_candidate_fingerprint_changes_when_location_set_changes():
    # 有新location加入同名家族 = 新问题，应该重新提示
    a = _fingerprint_of_only(
        build_alert_candidates("dongguan", {22: {"guanfogaosuchuzadao": 50}, 23: {"guanfogaosuchuzadao": 1}}),
        "merge_candidate",
    )
    b = _fingerprint_of_only(
        build_alert_candidates(
            "dongguan",
            {22: {"guanfogaosuchuzadao": 50}, 23: {"guanfogaosuchuzadao": 1}, 24: {"guanfogaosuchuzadao": 1}},
        ),
        "merge_candidate",
    )
    assert a != b


def test_merge_candidate_fingerprints_differ_per_site_text():
    # 同一组location_ids下两个不同的撞名site文本必须是两条不同的提示，
    # fingerprint不能撞(配合build_alert_candidates的seen_merge_candidates)
    candidates = build_alert_candidates("dongguan", {22: {"a": 1, "b": 1}, 23: {"a": 1, "b": 1}})
    merge_fps = {
        compute_votes_fingerprint(c.fingerprint_input)
        for c in candidates
        if c.alert_type == "merge_candidate"
    }
    assert len(merge_fps) == 2


def test_name_drift_and_merge_candidate_fingerprints_do_not_collide():
    # 同一批数据同时产生两类告警时，两者的fingerprint不能相同
    candidates = build_alert_candidates(
        "dongguan", {22: {"a": 5, "b": 1}, 23: {"a": 1, "b": 3}}
    )
    drift = [c for c in candidates if c.alert_type == "name_drift"]
    merge = [c for c in candidates if c.alert_type == "merge_candidate"]
    assert drift and merge
    drift_fps = {compute_votes_fingerprint(c.fingerprint_input) for c in drift}
    merge_fps = {compute_votes_fingerprint(c.fingerprint_input) for c in merge}
    assert not (drift_fps & merge_fps)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
