"""tuxun_agent.py — 与图寻服务器和 Google 街景交互的封装模块。

网络策略（国内可用性）：
* 图寻 API（tuxun.fun）：国内可直连，会话禁用 trust_env，不走系统代理；
* Google 街景图片（streetviewpixels-pa.googleapis.com）：国内被墙，
  下载会话遵循环境变量 PROXY_URL / HTTPS_PROXY / HTTP_PROXY，
  在 .env 中配置 PROXY_URL=http://127.0.0.1:7890 即可走本地代理（Clash 等）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

try:
    from requests.adapters import Retry
except ImportError:  # 兼容旧版 requests
    from urllib3.util.retry import Retry  # type: ignore

logger = logging.getLogger("tuxun.agent")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 相对初始朝向的水平视角
_YAW_4: List[Tuple[str, int]] = [("前", 0), ("右", 90), ("后", 180), ("左", 270)]
_YAW_8: List[Tuple[str, int]] = [
    ("前", 0), ("右前", 45), ("右", 90), ("右后", 135),
    ("后", 180), ("左后", 225), ("左", 270), ("左前", 315),
]


class TuxunAPIError(Exception):
    """图寻 API 调用失败（带人类可读的原因）。"""


def _normalize_cookie(cookie: str) -> str:
    """把各种常见 Cookie 输入统一成完整 Cookie 头字符串。

    支持四种写法：
    1. 完整 Cookie 头：  ``fun_ticket=xxx; SESSION=yyy``（任意平台的头都原样透传）
    2. 只有 fun_ticket 值：``x-xxxx...``
    3. 浏览器插件导出的 Cookie JSON（数组格式）字符串
    4. 指向上述 JSON 文件的路径（推荐，避免 .env 过长）
    """
    cookie = cookie.strip().strip('"').strip("'")
    # 4) JSON 文件路径
    if not cookie.startswith("[") and os.path.exists(cookie):
        with open(cookie, "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    # 3) Cookie 导出插件生成的 JSON 数组: [{"name": ..., "value": ...}, ...]
    if cookie.startswith("["):
        try:
            items = json.loads(cookie)
            pairs = [
                f"{item['name']}={item['value']}"
                for item in items
                if isinstance(item, dict) and item.get("name")
            ]
            if pairs:
                return "; ".join(pairs)
        except json.JSONDecodeError:
            pass
    # 1) 完整 Cookie 头（含多个键值对）原样透传 —— 图寻/GeoGuessr 通用
    if "=" in cookie and ";" in cookie:
        return cookie
    # 2) 单个裸值：视为 fun_ticket
    if "fun_ticket" in cookie:
        return cookie
    return f"fun_ticket={cookie}"


def _make_session(retries: int = 3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class TuxunAgent:
    """负责与 tuxun.fun 服务端交互，并构建/下载 Google 街景图片。"""

    def __init__(self, cookie: str, base_url: str = "https://tuxun.fun", timeout: int = 10):
        if not cookie or not cookie.strip():
            raise ValueError("Cookie 不能为空。")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 图寻 API：直连（trust_env=False，避免被系统/环境代理影响）
        self.session = _make_session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": _UA,
                "Content-Type": "application/json",
                "Cookie": _normalize_cookie(cookie),
                "Referer": self.base_url,
            }
        )

    # ------------------------------------------------------------------
    # 图寻 API
    # ------------------------------------------------------------------
    def verify_login(self) -> Optional[str]:
        """验证 Cookie 是否有效，成功返回用户 UID，失败返回 None。

        依次尝试三个只读端点（新版 profile -> 旧版 profile -> 历史对局），
        任一返回有效用户信息即视为登录成功。
        """
        logger.info("正在验证图寻 Cookie ...")
        probes = (
            ("/api/v0/tuxun/getProfile", self._uid_from_profile),
            ("/api/get_profile", self._uid_from_profile),
            ("/api/v0/tuxun/history/listSelf", self._uid_from_history),
        )
        for path, extractor in probes:
            try:
                resp = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
                if resp.status_code != 200:
                    continue
                uid = extractor(resp.json())
                if uid:
                    logger.info("Cookie 有效，当前用户 UID: %s（via %s）", uid, path)
                    return uid
            except requests.exceptions.RequestException as exc:
                logger.debug("端点 %s 请求失败: %s", path, exc)
            except ValueError:
                logger.debug("端点 %s 返回非 JSON", path)
        logger.error("Cookie 验证失败：所有端点均未返回有效用户信息，请重新抓取 Cookie。")
        return None

    @staticmethod
    def _uid_from_profile(data: dict) -> Optional[str]:
        data = data.get("data") or {}
        user = data.get("userAO") or data
        uid = user.get("userId") or data.get("userId")
        return str(uid) if uid else None

    @staticmethod
    def _uid_from_history(data: dict) -> Optional[str]:
        games = data.get("data") or []
        for item in games:
            if item.get("userId"):
                return str(item["userId"])
        return None

    def get_history(self, page: int = 1, page_size: int = 20) -> List[Dict]:
        """获取当前用户的历史对局列表（只读）。"""
        resp = self.session.get(
            f"{self.base_url}/api/v0/tuxun/history/listSelf",
            params={"page": page, "pageSize": page_size},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []

    def get_pano_info(self, pano_id: str, source: str = "qq_pano") -> Optional[Dict]:
        """通过图寻 mapProxy 查询全景元信息（lat/lng/相邻全景等，只读）。

        source: qq_pano -> getQQPanoInfo（参数名 pano）；
                google  -> getPanoInfo（参数名 panoId）。
        """
        if source == "qq_pano":
            url, params = "/api/v0/tuxun/mapProxy/getQQPanoInfo", {"pano": pano_id}
        else:
            url, params = "/api/v0/tuxun/mapProxy/getPanoInfo", {"panoId": pano_id}
        try:
            resp = self.session.get(f"{self.base_url}{url}", params=params, timeout=self.timeout)
            data = resp.json().get("data")
            return data or None
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.debug("getPanoInfo 查询失败: %s", exc)
            return None

    def get_game_info(self, game_id: str) -> Dict:
        """根据游戏 ID 拉取整局游戏的原始数据。

        优先使用单排端点 solo/get（返回的 rounds 在对局结束后带真实经纬度），
        失败时回退到通用端点 game/getContent。
        """
        logger.info("正在向图寻服务器请求游戏数据 (ID: %s) ...", game_id)
        last_error: Optional[TuxunAPIError] = None
        for path in ("/api/v0/tuxun/solo/get", "/api/v0/tuxun/game/getContent"):
            try:
                resp = self.session.get(
                    f"{self.base_url}{path}", params={"gameId": game_id}, timeout=self.timeout
                )
            except requests.exceptions.RequestException as exc:
                raise TuxunAPIError(f"网络请求失败: {exc}") from exc
            if resp.status_code == 404:
                last_error = TuxunAPIError("游戏ID未找到或已过期，请检查是否复制完整。")
                continue
            if resp.status_code in (401, 403):
                raise TuxunAPIError("服务器拒绝访问（401/403），Cookie 很可能已失效，请重新抓取。")
            if resp.status_code != 200:
                last_error = TuxunAPIError(f"服务器返回异常状态码 {resp.status_code}。")
                continue
            try:
                data = resp.json()
            except ValueError:
                last_error = TuxunAPIError(f"{path} 返回了非 JSON 数据。")
                continue
            game = (data or {}).get("data") or {}
            if game.get("rounds"):
                game["_endpoint"] = path
                return game
            last_error = TuxunAPIError(
                f"{path} 未返回回合信息（该游戏可能不存在、未开始或类型不支持）。"
            )
        if last_error:
            raise last_error
        raise TuxunAPIError("无法获取游戏数据。")

    @staticmethod
    def get_current_round(game: Dict) -> Tuple[int, str]:
        """从游戏数据中取最新回合编号与 panoId。"""
        rounds = game.get("rounds") or []
        index = len(rounds) - 1
        pano_id = (rounds[index] or {}).get("panoId") or ""
        return index + 1, pano_id

    def get_pano_id(self, game_id: str) -> Optional[str]:
        """便捷方法：游戏 ID -> 最新回合的 Pano ID。"""
        try:
            game = self.get_game_info(game_id)
        except TuxunAPIError as exc:
            logger.error("%s", exc)
            return None
        round_no, pano_id = self.get_current_round(game)
        if not pano_id:
            logger.error("第 %d 回合没有 Pano ID（可能尚未出图）。", round_no)
            return None
        logger.info("成功获取 Pano ID: %s（第 %d 回合）", pano_id, round_no)
        return pano_id

    # ------------------------------------------------------------------
    # Google 街景图片
    # ------------------------------------------------------------------
    @staticmethod
    def get_thumbnail_url(
        pano_id: str,
        yaw: int = 0,
        pitch: int = 0,
        width: int = 1024,
        height: int = 768,
        fov: int = 100,
    ) -> str:
        """根据 Pano ID 构建 Google 街景缩略图 URL。"""
        return (
            "https://streetviewpixels-pa.googleapis.com/v1/thumbnail"
            f"?panoid={pano_id}&cb_client=maps_sv.tactile.gps"
            f"&w={width}&h={height}&yaw={yaw}&pitch={pitch}&thumbfov={fov}"
        )

    @staticmethod
    def get_directional_image_urls(
        pano_id: str,
        count: int = 4,
        include_sky: bool = True,
        width: int = 1024,
        height: int = 768,
    ) -> List[Tuple[str, str]]:
        """构建各方向街景图片 URL 列表 [(标签, url), ...]。

        count=4: 前/右/后/左；count=8: 每 45° 一个方向。
        include_sky: 额外附加一张朝天（pitch=90）图，用于观察太阳方位与天象。
        """
        yaws = _YAW_4 if count <= 4 else _YAW_8
        urls = [
            (name, TuxunAgent.get_thumbnail_url(pano_id, yaw=yaw, width=width, height=height))
            for name, yaw in yaws[:count]
        ]
        if include_sky:
            urls.append(("天空", TuxunAgent.get_thumbnail_url(pano_id, yaw=0, pitch=90)))
        return urls

    @staticmethod
    def download_image(url: str, timeout: int = 15, retries: int = 3) -> bytes:
        """下载一张街景图片。

        使用独立会话并遵循代理环境变量（PROXY_URL / HTTPS_PROXY），
        以便国内用户通过本地代理访问 Google 图片服务。
        """
        session = _make_session(retries)  # trust_env 默认 True -> 会读取代理环境变量
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                resp = session.get(url, timeout=timeout)
                resp.raise_for_status()
                return resp.content
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                logger.warning("图片下载失败（第 %d/%d 次）: %s", attempt, retries, exc)
                if attempt < retries:
                    time.sleep(1.5 * attempt)
        raise TuxunAPIError(
            f"街景图片下载失败: {last_exc}\n"
            "如果是国内网络，请在 .env 中设置 PROXY_URL（例如 http://127.0.0.1:7890）后重试。"
        )
