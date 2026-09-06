"""geocode.py — 坐标系转换 + 逆地理编码（坐标 -> 文字地址）。

坐标系说明（国内可用性的关键）：
* Google 街景返回的坐标是 WGS84（国际标准 GPS 坐标）；
* 腾讯/高德地图使用 GCJ-02（国测局加密坐标，俗称"火星坐标"）；
* 高德逆地理编码接口只接受 GCJ-02 坐标，WGS84 直接传入会产生约 300~600 米偏移；
* Nominatim（OpenStreetMap）只接受 WGS84 坐标。

因此本模块提供：
* wgs84_to_gcj02 / gcj02_to_wgs84：标准火星坐标转换算法；
* reverse_geocode(..., coord="wgs84"|"gcj02")：统一入口，内部自动转换后再请求对应服务。

逆地理编码策略：
* 国内坐标：优先高德（需配置 amap_key，全中文、更精准）；
* 其余坐标 / 未配置高德 Key：Nominatim 免费接口（无需 Key）。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Dict

import requests

logger = logging.getLogger("tuxun.geocode")

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_AMAP_URL = "https://restapi.amap.com/v3/geocode/regeo"
_BIGDATACLOUD_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"

# 内置默认高德 Key（Web服务），开箱即用；可在 .env 中用 AMAP_KEY 覆盖为自己的
DEFAULT_AMAP_KEY = "61c71448ec236ec8432ce649b2edf7da"
DEFAULT_AMAP_JS_KEY = "27e999dda1560187e2eff2daab12e6a5"

_HEADERS = {
    # Nominatim 要求请求头中带有可识别应用的信息
    "User-Agent": "tuxun-helper/2.0 (https://github.com/yourname/tuxun-helper)",
}


def _resolve_amap_key(key: str = "") -> str:
    """空值时依次回退：环境变量 AMAP_KEY -> 内置默认 Key。"""
    if key:
        return key
    return os.getenv("AMAP_KEY", "").strip() or DEFAULT_AMAP_KEY

_cache: Dict[str, str] = {}
_lock = threading.Lock()
_last_request_at = 0.0
_MIN_INTERVAL = 1.1  # 秒，遵守 Nominatim 使用政策
_CACHE_MAX = 500


# ---------------------------------------------------------------------------
# GCJ-02（火星坐标）与 WGS84 互转 —— 公开的通用算法实现
# ---------------------------------------------------------------------------

_PI = math.pi
_X_PI = _PI * 3000.0 / 180.0
_A = 6378245.0                 # 长半轴
_EE = 0.00669342162296594323   # 偏心率平方


def _out_of_china(lat: float, lng: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320.0 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lng: float):
    """WGS84 -> GCJ-02。国外坐标原样返回。"""
    if _out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lat + dlat, lng + dlng


def gcj02_to_wgs84(lat: float, lng: float):
    """GCJ-02 -> WGS84（近似逆变换，误差在 1~2 米级，足够本工具使用）。"""
    if _out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lat * 2 - (lat + dlat), lng * 2 - (lng + dlng)


def bd09_to_gcj02(lat: float, lng: float):
    """BD09（百度坐标）-> GCJ-02。国外坐标原样返回。"""
    if _out_of_china(lat, lng):
        return lat, lng
    x, y = lng - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.sin(theta), z * math.cos(theta)


def gcj02_to_bd09(lat: float, lng: float):
    """GCJ-02 -> BD09（百度坐标）。"""
    if _out_of_china(lat, lng):
        return lat, lng
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


def _to_wgs84(lat: float, lng: float, coord: str):
    if coord == "bd09":
        lat, lng = bd09_to_gcj02(lat, lng)
    return gcj02_to_wgs84(lat, lng) if coord in ("gcj02", "bd09") else (lat, lng)


def _to_gcj02(lat: float, lng: float, coord: str):
    if coord == "bd09":
        return bd09_to_gcj02(lat, lng)
    return wgs84_to_gcj02(lat, lng) if coord == "wgs84" else (lat, lng)


# ---------------------------------------------------------------------------
# 逆地理编码
# ---------------------------------------------------------------------------

def _is_in_china(lat: float, lng: float) -> bool:
    """粗略判断坐标是否落在中国陆地范围（用于选择逆地理编码服务）。"""
    return 3.0 <= lat <= 55.0 and 73.0 <= lng <= 136.0


def _cache_get(key: str) -> str:
    with _lock:
        return _cache.get(key, "")


def _cache_put(key: str, value: str) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    with _lock:
        _cache[key] = value


def regeo_nominatim(lat: float, lng: float) -> str:
    """通过 Nominatim 反查地址（入参必须是 WGS84），失败返回空字符串。"""
    global _last_request_at
    key = f"osm:{round(lat, 4)}:{round(lng, 4)}"
    cached = _cache_get(key)
    if cached:
        return cached
    with _lock:
        # 全局限速，保证两次请求间隔 >= _MIN_INTERVAL
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "zoom": 16,
                "addressdetails": 1,
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return ""
        a = data.get("address", {})
        parts = [
            a.get("country"),
            a.get("state") or a.get("province"),
            a.get("city") or a.get("town") or a.get("village") or a.get("municipality"),
            a.get("suburb") or a.get("district"),
            a.get("road"),
        ]
        address = "".join(p for p in parts if p)
        if not address:
            address = data.get("display_name", "")
        _cache_put(key, address)
        return address
    except Exception as exc:
        logger.debug("Nominatim 反查失败 (%s, %s): %s", lat, lng, exc)
        return ""


def regeo_bigdatacloud(lat: float, lng: float) -> str:
    """通过 BigDataCloud 免费接口反查海外地址（无需 Key，Nominatim 的备用源），失败返回空字符串。"""
    key = f"bdc:{round(lat, 4)}:{round(lng, 4)}"
    cached = _cache_get(key)
    if cached:
        return cached
    try:
        resp = requests.get(
            _BIGDATACLOUD_URL,
            params={"latitude": lat, "longitude": lng, "localityLanguage": "zh"},
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=10,
        )
        resp.raise_for_status()
        a = resp.json()
        parts = [
            a.get("countryName"),
            a.get("principalSubdivision"),
            a.get("city"),
            a.get("locality"),
        ]
        address = "".join(p for p in parts if p)
        _cache_put(key, address)
        return address
    except Exception as exc:
        logger.debug("BigDataCloud 反查失败 (%s, %s): %s", lat, lng, exc)
        return ""


def regeo_amap(lat: float, lng: float, key: str) -> str:
    """通过高德 API 反查地址（入参必须是 GCJ-02，仅对国内坐标有效），失败返回空字符串。"""
    if not key:
        return ""
    cache_key = f"amap:{round(lat, 5)}:{round(lng, 5)}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    try:
        resp = requests.get(
            _AMAP_URL,
            params={"location": f"{lng},{lat}", "key": key, "extensions": "base"},
            headers={"User-Agent": _HEADERS["User-Agent"]},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "1":
            return ""
        c = data["regeocode"]["addressComponent"]
        parts = [
            c.get("province"),
            c.get("city") if isinstance(c.get("city"), str) else "",
            c.get("district") if isinstance(c.get("district"), str) else "",
            c.get("township") if isinstance(c.get("township"), str) else "",
        ]
        address = "".join(p for p in parts if p)
        _cache_put(cache_key, address)
        return address
    except Exception as exc:
        logger.debug("高德反查失败 (%s, %s): %s", lat, lng, exc)
        return ""


def reverse_geocode(lat: float, lng: float, amap_key: str = "", coord: str = "wgs84") -> str:
    """逆地理编码统一入口。

    coord: 传入坐标的坐标系，"wgs84"（Google 来源）、"gcj02"（腾讯街景来源）
    或 "bd09"（百度街景来源）。内部会自动转换到目标服务所需的坐标系。
    amap_key 留空时自动使用环境变量 AMAP_KEY 或内置默认 Key（仅国内坐标会请求高德）。
    """
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return ""
    key = _resolve_amap_key(amap_key)
    if key and _is_in_china(lat, lng):
        glat, glng = _to_gcj02(lat, lng, coord)
        address = regeo_amap(glat, glng, key)
        if address:
            return address
    wlat, wlng = _to_wgs84(lat, lng, coord)
    address = regeo_nominatim(wlat, wlng)
    if not address:
        # Nominatim 在国内网络时常不可达，回退到 BigDataCloud
        address = regeo_bigdatacloud(wlat, wlng)
    return address


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间大圆距离（千米）。用于复盘时计算 AI 猜测与真实答案的误差。"""
    lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def amap_uri_link(lat: float, lng: float, coord: str = "wgs84") -> str:
    """生成高德地图网页链接（高德链接需要 GCJ-02 坐标）。"""
    glat, glng = _to_gcj02(lat, lng, coord)
    return f"https://uri.amap.com/marker?position={glng:.6f},{glat:.6f}"


def osm_link(lat: float, lng: float) -> str:
    """生成 OpenStreetMap 链接（WGS84）。"""
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=16/{lat:.6f}/{lng:.6f}"


def google_maps_link(lat: float, lng: float) -> str:
    """生成 Google Maps 链接（WGS84）。"""
    return f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}"
