"""
飞行会话文件夹命名规范：city_site_drone_height_person_date，
例如 changchun_panshiroad_m3e_200m_wangziyu_20260912。
2026-10-07起规范改版，全部字段一律小写(之前City/Site要求首字母大写，
已废弃)，且Mavic3E统一缩写成m3e——这是第三版规范了，改动点都记在这，
以后再变就对照这里看哪些字段真的变了。

被两边共用，所以单独拆出来，不然scanner.py想复用aliyun_sync.py这边的正则，
会跟aliyun_sync.py导入scanner.py的run_scan形成循环import：
  - services/aliyun_sync.py：校验云端文件夹命名、算本地落盘用的分类目录
  - services/scanner.py：扫盘时从storage_path反推设备型号/采集人，回填
    videos.device_id/collected_by(见_apply_session_metadata)

字段含义：
  city   采集地点城市，拼音，全部小写，如 beijing
  site   地点名，拼音，全部小写，如 panshiroad
  drone  设备型号，固定白名单(SESSION_FOLDER_DEVICES)，全部小写，不在
         名单里的一律拒绝——宁可先报错拦下来，也不要静默收了一个拼错/
         没见过的机型名。Mavic3E统一写m3e(不是mavic3e)
  height 采集高度，数字+小写m，如 200m
  person 采集人员姓名拼音，全部小写，如 wangziyu
  date   上传日期 YYYYMMDD

倾斜摄影数据(.zip，不是文件夹，文件名加_3dgs后缀)这部分还没实现——
aliyun_sync.py目前只处理文件夹，不处理游离在文件夹外的.zip文件，
这类文件会被当成"散装文件"直接忽略，不会报错也不会下载。
"""
from __future__ import annotations

import re
from pathlib import Path

SESSION_FOLDER_DEVICES = ("m3e", "air2s", "air3s", "mini4pro", "mini3pro")
SESSION_FOLDER_RE = re.compile(
    r"^(?P<city>[a-z]+)_(?P<site>[a-z]+)_"
    r"(?P<drone>" + "|".join(SESSION_FOLDER_DEVICES) + r")_"
    r"(?P<height>\d+)m_(?P<person>[a-z]+)_(?P<date>\d{8})$"
)


def looks_like_session_candidate(name: str) -> bool:
    """名字里有没有出现过设备型号白名单里的词——用来判断这个文件夹"像"是在
    想当会话文件夹、只是没完全写对(该报错拦下来)，还是纯粹的组织性目录
    (该继续往下探索)。按"_"分段做整段匹配，不是子串匹配，避免误伤；大小写
    不敏感——"M3E"这种大小写写错的也该被拦下来报错，而不是因为没有
    精确匹配上白名单就被当成无关目录静默放过。"""
    return any(segment.lower() in SESSION_FOLDER_DEVICES for segment in name.split("_"))


def diagnose_session_folder_name(name: str) -> list[str]:
    """给命名不规范的候选文件夹做字段级别的诊断，返回具体哪几个字段哪里错了
    (而不是笼统地说"不符合规范")，报警/log都用这个，方便人一眼定位要改哪里。"""
    parts = name.split("_")
    if len(parts) != 6:
        return [f"应该是 city_site_drone_height_person_date 共6段(用_分隔)，实际是{len(parts)}段"]
    city, site, drone, height, person, date = parts
    reasons = []
    if not re.fullmatch(r"[a-z]+", city):
        reasons.append(f"city字段{city!r}应为拼音且全部小写，如beijing")
    if not re.fullmatch(r"[a-z]+", site):
        reasons.append(f"site字段{site!r}应为拼音且全部小写，如panshiroad")
    if drone not in SESSION_FOLDER_DEVICES:
        devices = "/".join(SESSION_FOLDER_DEVICES)
        reasons.append(f"drone字段{drone!r}不在设备白名单里，只认{devices}(Mavic3E请写成m3e)")
    if not re.fullmatch(r"\d+m", height):
        reasons.append(f"height字段{height!r}应为数字+小写m，如200m")
    if not re.fullmatch(r"[a-z]+", person):
        reasons.append(f"person字段{person!r}应全部小写，如wangziyu")
    if not re.fullmatch(r"\d{8}", date):
        reasons.append(f"date字段{date!r}应为8位数字YYYYMMDD，如20260912")
    return reasons or ["跟规范整体对不上，但逐字段检查没发现具体问题，可能是分隔符本身有问题"]


def derive_category_folder(match: re.Match) -> str:
    """从会话文件夹名解析出的城市+日期，算出本地落盘要用的分类文件夹名
    (年份_城市_AerialVideos_季度)。这一层云端不存在，是我们本地自己拼的。"""
    date = match.group("date")
    year = date[:4]
    quarter = (int(date[4:6]) - 1) // 3 + 1
    return f"{year}_{match.group('city')}_AerialVideos_Q{quarter}"


def find_session_match(path: str | Path) -> re.Match | None:
    """在一个本地文件系统路径里找符合会话命名规范的那一段(深度不固定，
    m3e系列是倒数第二层，mini/air系列可能更靠外)，找到第一个匹配就返回。
    用于扫盘时从storage_path反推city/site/drone/height/person/date，
    补全videos.device_id/collected_by，见scanner.py的_apply_session_metadata。"""
    for part in Path(path).parts:
        match = SESSION_FOLDER_RE.match(part)
        if match:
            return match
    return None
