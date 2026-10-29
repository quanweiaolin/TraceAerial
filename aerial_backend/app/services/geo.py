"""GPS坐标转局部平面坐标 + 三维距离计算，给地址聚类(backfill脚本的complete
linkage一次性初始化，和scanner.py在线的leader算法)共用，避免两处各写一份。

20米这种小尺度上，地球曲率用等经纬线近似(flat-plane/equirectangular)
误差在亚毫米级，足够用，不需要为此引入完整的测地库(比如pyproj)。
"""
from __future__ import annotations

import math

EARTH_RADIUS_METERS = 6371000.0


def latlon_to_local_meters(
    lon: float, lat: float, ref_lon: float, ref_lat: float
) -> tuple[float, float]:
    """把(lon, lat)转换成相对于参考点(ref_lon, ref_lat)的局部平面坐标(米)，
    (x=东西方向, y=南北方向)。cos(纬度)用两点纬度的平均值近似——只要两点
    本身距离不太远(聚类场景里我们只关心几十米量级的pair)，这个近似就足够
    精确，参考点选多远都不影响同一对点之间算出来的相对距离。"""
    avg_lat_rad = math.radians((lat + ref_lat) / 2)
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_METERS * math.cos(avg_lat_rad)
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_METERS
    return x, y


def distance_3d_meters(
    lon1: float, lat1: float, height1: float,
    lon2: float, lat2: float, height2: float,
) -> float:
    """两点间的三维距离(米)：水平分量用等经纬线近似，垂直分量(高度差)
    直接相减，两者做欧氏合成。"""
    x, y = latlon_to_local_meters(lon1, lat1, lon2, lat2)
    horizontal = math.hypot(x, y)
    vertical = height1 - height2
    return math.hypot(horizontal, vertical)
