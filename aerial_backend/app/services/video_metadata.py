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
import subprocess
from datetime import datetime, timezone
from pathlib import Path


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


def extract_new_video_fields(path: str | Path) -> dict:
    """整合probe_video + GPS提取，返回能直接拆包进Video(...)/UPDATE的字段dict。

    新视频入库(scanner.py)和给老视频补字段的回填脚本(services/backfill.py)
    共用这一个函数，避免两处重复维护同一套提取逻辑。
    """
    meta = probe_video(path)
    # collected_at优先用视频容器自带的creation_time标签，没有就退回文件mtime
    collected_at = meta["collected_at"] or datetime.fromtimestamp(
        Path(path).stat().st_mtime, tz=timezone.utc
    )

    # GPS：同名.srt存在就尝试解析，解析不出来(没有srt/格式不认识)保持None
    collect_lon = collect_lat = None
    srt_path = find_srt_sidecar(path)
    if srt_path is not None:
        gps = extract_gps_from_srt(srt_path)
        if gps is not None:
            collect_lon, collect_lat = gps

    return {
        "duration_seconds": meta["duration_seconds"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "codec": meta["codec"],
        "collected_at": collected_at,
        "collect_lon": collect_lon,
        "collect_lat": collect_lat,
    }


def extract_gps_from_srt(srt_path: str | Path) -> tuple[float, float] | None:
    """从SRT遥测字幕里提取一个代表性GPS坐标(取第一条能解析出来的记录)。

    返回 (lon, lat)，对应 videos.collect_lon / videos.collect_lat；
    解析不出来(格式不认识/文件读取失败)时返回None。

    新增机型/格式支持：在这里加正则+分支即可，不用改调用方代码。
    """
    try:
        text = Path(srt_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    kv_match = _GPS_KV_PATTERN.search(text)
    if kv_match:
        lat, lon = float(kv_match.group(1)), float(kv_match.group(2))
        return lon, lat

    func_match = _GPS_FUNC_PATTERN.search(text)
    if func_match:
        lon, lat = float(func_match.group(1)), float(func_match.group(2))
        return lon, lat

    return None
