"""
视频技术元数据 + GPS坐标提取。

技术元数据(时长/分辨率/帧率/编码)用ffprobe读文件头，不需要解码整个文件，
即使是几十GB的航测视频也是毫秒级返回，不会像sha256那样要通读一遍。

GPS坐标(collect_lon/collect_lat)的思路：很多无人机拍摄时会生成同名.srt
遥测字幕文件(和视频同目录同名，仅后缀不同)，逐帧记录经纬度/高度等。这里做成
"文件存在就尝试解析，解析不出来就返回None"的探测式接口，不影响主流程——
调用方(scanner.py)按None处理即可，视频照常入库。

当前只覆盖了两种常见DJI SRT格式，遇到解析不出来的新格式，
在这个文件里加正则+分支即可，不用改调用方代码。
"""
from __future__ import annotations

import json
import re
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.services.geo import trajectory_diameter_meters


def probe_video(path: str | Path) -> dict:
    """用ffprobe读取视频技术元数据。
    返回 duration_seconds/width/height/fps/codec/collected_at，
    读取失败(ffprobe报错/字段缺失/文件损坏)时对应字段为None，不抛异常，
    避免个别文件有问题时中断整个扫盘。
    """
    result: dict = {
        "duration_seconds": None,
        "width": None,
        "height": None,
        "fps": None,
        "codec": None,
        "collected_at": None,
    }
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return result

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    fmt = data.get("format", {})

    if video_stream:
        result["width"] = video_stream.get("width")
        result["height"] = video_stream.get("height")
        result["codec"] = video_stream.get("codec_name")
        fps_str = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        if fps_str and fps_str != "0/0":
            num, _, den = fps_str.partition("/")
            try:
                result["fps"] = float(num) / float(den) if den else float(num)
            except (ValueError, ZeroDivisionError):
                pass

    if fmt.get("duration"):
        try:
            result["duration_seconds"] = float(fmt["duration"])
        except ValueError:
            pass

    creation_time = fmt.get("tags", {}).get("creation_time")
    if creation_time:
        try:
            result["collected_at"] = datetime.fromisoformat(
                creation_time.replace("Z", "+00:00")
            )
        except ValueError:
            pass

    return result


def find_srt_sidecar(video_path: str | Path) -> Path | None:
    """同目录下找与视频同名(仅扩展名不同)的字幕文件，大小写都试一下。"""
    video_path = Path(video_path)
    for ext in (".srt", ".SRT"):
        candidate = video_path.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


# 较新DJI机型(如Mavic 3/Air 2S)逐帧KV对格式：
# ... [latitude: 22.543099] [longitude: 114.057868] ...
_GPS_KV_PATTERN = re.compile(
    r"\[latitude\s*:\s*(-?\d+\.?\d*)\]\s*\[longitude\s*:\s*(-?\d+\.?\d*)\]"
)
# 早期DJI机型(如Phantom/Mavic Pro) GPS(lon,lat,alt)函数式格式
_GPS_FUNC_PATTERN = re.compile(r"GPS\(\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*,")

# 较新DJI机型同一帧里还带 rel_alt(相对起飞点的高度)/abs_alt(海拔高度，不用)，
# 比如 [rel_alt: 101.500 abs_alt: 151.500]——地址聚类(见scanner.py的
# _assign_city_site)要的是实际飞行高度，rel_alt才对，abs_alt混入了地面
# 本身的海拔，不能用。早期GPS(lon,lat,alt)函数式格式里的alt到底是相对
# 高度还是绝对高度，这次没有真机样本验证过，所以这里先不解析——srt_height
# 对老格式视频永久是None，不是bug，等拿到老格式真机样本确认了再补
_REL_ALT_PATTERN = re.compile(r"\[rel_alt\s*:\s*(-?\d+\.?\d*)")

# 跟_GPS_KV_PATTERN/_REL_ALT_PATTERN分开独立匹配不同：这个要求经纬度+
# rel_alt在同一帧(同一行)里绑在一起，给飞行稳定性判定(is_stable)专用——
# 判定"整段飞行有没有明显移动"需要同一帧的位置和高度对应起来，不能像
# 代表性坐标/高度那样经纬度和高度分别独立取中位数。中间允许隔着iso/
# shutter等其它字段(.*?不跨行，不会误吃到下一帧的数据)
_TRAJECTORY_KV_PATTERN = re.compile(
    r"\[latitude\s*:\s*(-?\d+\.?\d*)\]\s*\[longitude\s*:\s*(-?\d+\.?\d*)\].*?"
    r"\[rel_alt\s*:\s*(-?\d+\.?\d*)"
)


def extract_srt_fields(path: str | Path, max_drift_meters: float) -> dict:
    """只做SRT相关的提取(经纬度/高度/飞行稳定性)，不碰probe_video的ffprobe
    调用——专门给"只想快速刷新这几个字段，不想为了技术元数据重新读一遍
    整个视频文件头"的场景用(比如scripts/refresh_srt_fields.py，ffprobe
    在NAS上对几万条视频逐个起子进程很慢，但SRT只是个小文本文件，读取
    成本低得多)。extract_new_video_fields内部也是调这个，两处共用一份
    逻辑，不重复维护。

    max_drift_meters: 判定is_stable用的最大位移阈值(米)，调用方自己决定
    从哪里拿这个值(scanner.py/backfill.py从app.config.settings读，独立
    脚本走CLI参数)——这个模块本身不依赖app.config，保持可以脱离.env
    独立运行。
    """
    collect_lon = collect_lat = None
    srt_height = None
    is_stable = None
    srt_path = find_srt_sidecar(path)
    if srt_path is not None:
        gps = extract_gps_from_srt(srt_path)
        if gps is not None:
            collect_lon, collect_lat = gps
        srt_height = extract_rel_alt_from_srt(srt_path)
        trajectory = extract_srt_trajectory(srt_path)
        if trajectory is not None:
            is_stable = trajectory_diameter_meters(trajectory) <= max_drift_meters

    return {
        "collect_lon": collect_lon,
        "collect_lat": collect_lat,
        "srt_height": srt_height,
        "is_stable": is_stable,
    }


def extract_new_video_fields(path: str | Path, max_drift_meters: float = 30.0) -> dict:
    """整合probe_video + SRT提取(extract_srt_fields)，返回能直接拆包进
    Video(...)/UPDATE的字段dict。

    新视频入库(scanner.py)和给老视频补字段的回填脚本(services/backfill.py)
    共用这一个函数，避免两处重复维护同一套提取逻辑。
    """
    meta = probe_video(path)
    # collected_at优先用视频容器自带的creation_time标签，没有就退回文件mtime
    collected_at = meta["collected_at"] or datetime.fromtimestamp(
        Path(path).stat().st_mtime, tz=timezone.utc
    )

    return {
        "duration_seconds": meta["duration_seconds"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "codec": meta["codec"],
        "collected_at": collected_at,
        **extract_srt_fields(path, max_drift_meters),
    }


def extract_gps_from_srt(srt_path: str | Path) -> tuple[float, float] | None:
    """从SRT遥测字幕里提取一个代表性GPS坐标：经度/纬度分别取全程所有能
    解析出来的读数的中位数，不是只取第一条。

    2026-10-29跟srt_height同一次改动、同一个原因：起飞爬升阶段/还没飞到
    真正拍摄点时录的SRT，开头那几条经纬度可能还是起飞点附近的坐标，不是
    这段视频真正拍摄的位置，只取第一条容易取到这类瞬态值(跟rel_alt取值
    偏低是同一个起飞爬升阶段的症状，这里会同时影响水平位置)。中位数能
    滤掉这种首尾瞬态值。

    返回 (lon, lat)，对应 videos.collect_lon / videos.collect_lat；
    解析不出来(格式不认识/文件读取失败)时返回None。

    新增机型/格式支持：在这里加正则+分支即可，不用改调用方代码。
    """
    try:
        text = Path(srt_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    kv_matches = _GPS_KV_PATTERN.findall(text)
    if kv_matches:
        lon = statistics.median(float(lon_str) for _, lon_str in kv_matches)
        lat = statistics.median(float(lat_str) for lat_str, _ in kv_matches)
        return lon, lat

    func_matches = _GPS_FUNC_PATTERN.findall(text)
    if func_matches:
        lon = statistics.median(float(lon_str) for lon_str, _ in func_matches)
        lat = statistics.median(float(lat_str) for _, lat_str in func_matches)
        return lon, lat

    return None


def extract_rel_alt_from_srt(srt_path: str | Path) -> float | None:
    """从SRT遥测字幕里提取一个代表性飞行高度：取全程所有能解析出来的rel_alt
    读数的中位数，不是只取第一条。

    2026-10-29地址聚类backfill跑出来的真实数据发现过"取第一条"的问题：
    起飞爬升阶段还没到巡航高度时录的SRT，第一条rel_alt可能只有10米量级，
    跟同地点其他视频普遍119-120米差一大截，把这个瞬时值当成整段视频的
    代表高度会直接导致聚类判断错误(南宁xiuxiangavenueqingxiangexpressway
    那次实际案例)。爬升/下降阶段只占整段视频一小部分，中位数能自然滤掉
    这种首尾瞬态值。

    返回值对应 videos.srt_height；只认新格式的rel_alt标签，解析不出来
    (没有srt/是老格式/字段缺失)时返回None。"""
    try:
        text = Path(srt_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    matches = _REL_ALT_PATTERN.findall(text)
    if not matches:
        return None
    return statistics.median(float(v) for v in matches)


def extract_srt_trajectory(srt_path: str | Path) -> list[tuple[float, float, float]] | None:
    """解析整段SRT里每一帧的(lon, lat, rel_alt)三元组——跟extract_gps_from_srt/
    extract_rel_alt_from_srt分别独立取中位数不同，这里同一帧的坐标和高度
    绑在一起，保留"同一时刻"的对应关系，给is_stable(判定整段飞行有没有
    明显移动，见app.services.geo.trajectory_diameter_meters)专用。

    只支持新格式(带rel_alt的KV格式)；老格式没有解析过rel_alt，解析不出来
    (没有srt/是老格式/一帧都凑不出完整的latitude+longitude+rel_alt三元组)
    时返回None，is_stable对这些视频永久是None(未知)，不是False。"""
    try:
        text = Path(srt_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    matches = _TRAJECTORY_KV_PATTERN.findall(text)
    if not matches:
        return None
    return [(float(lon_str), float(lat_str), float(alt_str)) for lat_str, lon_str, alt_str in matches]
