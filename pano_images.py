"""pano_images.py — 国内图源（腾讯街景）全景图获取与方向视图裁剪。

腾讯街景瓦片无需任何 Key 即可直连：
    https://sv{0-3}.map.qq.com/tile?svid={panoId}&level=2&x={x}&y={y}&from=web&ch=0
    level>=2 均为最高清晰度：8 列 x 4 行 512px 瓦片 = 4096x2048 等距圆柱投影全景。

方向视图：在等距圆柱图上按罗盘朝向裁剪 90° 视窗（x = heading/360 * 宽度），
与游戏内"前/右/后/左"一致；天空视图取顶部条带。

百度街景暂未找到无需凭证的公开取图通道，遇到百度图源回合会给出明确提示。
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("tuxun.pano")

QQ_TILE_HOSTS = ["sv0.map.qq.com", "sv1.map.qq.com", "sv2.map.qq.com", "sv3.map.qq.com"]
QQ_GRID_COLS, QQ_GRID_ROWS, QQ_TILE_SIZE = 8, 4, 512

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://map.qq.com/",
}


class PanoImageError(Exception):
    """街景图获取失败。"""


class UnsupportedSourceError(Exception):
    """该图源暂不支持自动取图。"""


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def fetch_qq_equirect(pano_id: str, timeout: int = 12, workers: int = 8):
    """下载并拼接腾讯街景 4096x2048 全景图，返回 PIL.Image。"""
    try:
        from PIL import Image
    except ImportError:
        raise PanoImageError("处理腾讯街景需要 Pillow 库：pip install pillow")

    session = _make_session()
    canvas = Image.new("RGB", (QQ_GRID_COLS * QQ_TILE_SIZE, QQ_GRID_ROWS * QQ_TILE_SIZE), (0, 0, 0))
    coords = [(x, y) for y in range(QQ_GRID_ROWS) for x in range(QQ_GRID_COLS)]

    def fetch(coord: Tuple[int, int]):
        x, y = coord
        host = QQ_TILE_HOSTS[(x + y) % len(QQ_TILE_HOSTS)]
        url = (
            f"https://{host}/tile?svid={pano_id}"
            f"&level=2&x={x}&y={y}&from=web&ch=0"
        )
        last_exc: Optional[Exception] = None
        for _ in range(3):
            try:
                resp = session.get(url, timeout=timeout)
                if resp.status_code == 200 and len(resp.content) > 500:
                    return coord, Image.open(io.BytesIO(resp.content)).convert("RGB")
                last_exc = PanoImageError(f"瓦片 HTTP {resp.status_code}")
            except requests.exceptions.RequestException as exc:
                last_exc = exc
        raise PanoImageError(f"腾讯街景瓦片 ({x},{y}) 下载失败: {last_exc}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for coord, tile_img in pool.map(fetch, coords):
            canvas.paste(tile_img, (coord[0] * QQ_TILE_SIZE, coord[1] * QQ_TILE_SIZE))
    return canvas


def yaw_crop(
    img,
    heading_deg: float,
    fov_deg: int = 90,
    size: int = 640,
    sky: bool = False,
) -> bytes:
    """从等距圆柱全景中裁剪指定朝向的视图，返回 JPEG 字节。

    heading: 罗盘朝向（0=北，顺时针）。sky=True 时改为裁剪该朝向的顶部天空条带。
    """
    from PIL import Image

    width, height = img.size
    fov_px = int(width * fov_deg / 360)

    if sky:
        # 顶部条带：与朝向对齐的水平中心 + 顶部 1/4 高度
        cx = int((heading_deg % 360) / 360 * width)
        left = cx - fov_px // 2
        box = (left, 0, left + fov_px, height // 4)
        crop = _wrap_crop(img, box)
        crop = crop.resize((size, size * crop.height // crop.width))
    else:
        cx = int((heading_deg % 360) / 360 * width)
        cy = height // 2 + height // 16  # 略微下移，地平线在画面中更自然
        half = fov_px // 2
        box = (cx - half, cy - half, cx + half, cy + half)
        crop = _wrap_crop(img, box)
        crop = crop.resize((size, size))

    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _wrap_crop(img, box: Tuple[int, int, int, int]):
    """支持水平回绕的裁剪（全景 x 轴是环形的）。"""
    from PIL import Image

    left, top, right, bottom = box
    width = img.width
    if left >= 0 and right <= width:
        return img.crop(box)
    shifted = Image.new("RGB", (width * 2, img.height))
    shifted.paste(img, (0, 0))
    shifted.paste(img, (width, 0))
    return shifted.crop((left % width + (width if left < 0 else 0), top,
                         left % width + (width if left < 0 else 0) + (right - left), bottom))


def directional_views(
    pano_id: str,
    center_heading: float = 0.0,
    count: int = 4,
    include_sky: bool = True,
    size: int = 640,
) -> List[Tuple[str, bytes]]:
    """获取腾讯街景多方向视图 [(标签, JPEG字节), ...]，方向相对 initial heading。"""
    yaws = ([("前", 0), ("右", 90), ("后", 180), ("左", 270)] if count <= 4 else
            [("前", 0), ("右前", 45), ("右", 90), ("右后", 135),
             ("后", 180), ("左后", 225), ("左", 270), ("左前", 315)])
    img = fetch_qq_equirect(pano_id)
    views: List[Tuple[str, bytes]] = []
    for name, yaw in yaws[:count]:
        heading = (float(center_heading or 0) + yaw) % 360
        views.append((name, yaw_crop(img, heading, size=size)))
    if include_sky:
        heading = float(center_heading or 0)
        views.append(("天空", yaw_crop(img, heading, size=size, sky=True)))
    return views


def views_for_source(
    source: str,
    pano_id: str,
    heading: Optional[float] = None,
    count: int = 4,
    include_sky: bool = True,
    size: int = 640,
) -> List[Tuple[str, bytes]]:
    """按图源分发取图。目前支持 google_pano（由 tuxun_agent 处理）与 qq_pano。"""
    if source in ("qq_pano", "qq"):
        return directional_views(pano_id, center_heading=heading or 0.0,
                                 count=count, include_sky=include_sky, size=size)
    if source in ("baidu_pano", "baidu"):
        raise UnsupportedSourceError(
            "百度街景暂无公开免凭证取图通道，暂不支持 AI 取图。"
            "可使用实时取点模式（tuxun_proxy.py）或等对局结束后用复盘模式。"
        )
    raise UnsupportedSourceError(f"图源 {source} 请走 Google 街景缩略图通道。")
