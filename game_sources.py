"""game_sources.py — 对局数据源抽象层（三种检测模式的实现基础）。

三种模式：
* ``tuxun`` —— 单图寻：只走图寻 API；
* ``geoguessr`` —— 单 GeoGuessr：只走 GeoGuessr API（架构已预留，待实现）；
* ``auto`` —— 混合检测：根据用户输入自动识别来源（粘贴 URL 按域名判断；
  纯 ID 则按 [图寻 -> GeoGuessr] 顺序逐一尝试）。

新增一个平台只需实现 GameSource 接口并注册到 SOURCES。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tuxun_agent import TuxunAgent, TuxunAPIError, _normalize_cookie

# 图寻回合图源 -> 坐标系
_TUXUN_COORD_SYS = {
    "google_pano": "wgs84",
    "qq_pano": "gcj02",
    "baidu_pano": "bd09",
}

_SOURCE_LABEL = {
    "google_pano": "谷歌街景",
    "qq_pano": "腾讯街景",
    "baidu_pano": "百度街景",
    "google": "谷歌街景",
    "unknown": "未知图源",
}


def _decode_geo_pano(pid: str) -> str:
    """GeoGuessr API 返回的 panoId 是 hex 编码的 ASCII 字符串，解码为真实 Google Pano ID。"""
    if pid and len(pid) >= 20 and re.fullmatch(r"[0-9a-fA-F]+", pid):
        try:
            decoded = bytes.fromhex(pid).decode("ascii")
            if decoded.isprintable() and len(decoded) >= 10:
                return decoded
        except ValueError:
            pass
    return pid


def source_label(source: str) -> str:
    return _SOURCE_LABEL.get(source, source or "未知图源")


@dataclass
class RoundInfo:
    """一个回合的规整化信息。"""

    round_no: int
    pano_id: str = ""
    source: str = "unknown"        # google_pano / qq_pano / baidu_pano / ...
    coord_sys: str = "wgs84"       # wgs84 / gcj02 / bd09
    heading: Optional[float] = None
    lat: Optional[float] = None    # 对局结束后一般才有真实坐标（复盘用）
    lng: Optional[float] = None


@dataclass
class GameInfo:
    """一局游戏的规整化信息。"""

    platform: str                  # tuxun / geoguessr
    game_id: str
    status: str = "unknown"        # waiting / playing / finish / ...
    rounds: List[RoundInfo] = field(default_factory=list)
    score: Optional[float] = None
    raw: Dict = field(default_factory=dict)

    @property
    def finished_with_truth(self) -> bool:
        """是否为已结束且拿到真实坐标的对局（可直接复盘，无需 AI）。"""
        return bool(self.rounds) and all(
            r.lat is not None and r.lng is not None for r in self.rounds
        )

    @property
    def current_round(self) -> Optional[RoundInfo]:
        for r in self.rounds:
            if r.lat is None or r.lng is None:
                return r
        return self.rounds[-1] if self.rounds else None


class GameSource(ABC):
    """对局数据源接口。"""

    name: str = "abstract"

    @abstractmethod
    def verify(self) -> bool:
        """校验凭证是否可用。"""

    @abstractmethod
    def get_game(self, game_id: str) -> GameInfo:
        """拉取并规整化一局游戏。"""


# ---------------------------------------------------------------------------
# 图寻
# ---------------------------------------------------------------------------

class TuxunSource(GameSource):
    name = "tuxun"

    def __init__(self, cookie: str):
        self.agent = TuxunAgent(cookie)

    def verify(self) -> bool:
        return self.agent.verify_login() is not None

    def get_game(self, game_id: str) -> GameInfo:
        raw = self.agent.get_game_info(game_id)
        rounds: List[RoundInfo] = []
        for idx, r in enumerate(raw.get("rounds") or [], start=1):
            source = r.get("source") or "unknown"
            lat, lng = r.get("lat"), r.get("lng")
            rounds.append(
                RoundInfo(
                    round_no=r.get("round") or idx,
                    pano_id=str(r.get("panoId") or ""),
                    source=source if source != "google" else "google_pano",
                    coord_sys=_TUXUN_COORD_SYS.get(source, "wgs84"),
                    heading=r.get("heading"),
                    lat=float(lat) if lat is not None else None,
                    lng=float(lng) if lng is not None else None,
                )
            )
        players = raw.get("players") or []
        my_score = next(
            (p.get("score") for p in players if isinstance(p, dict) and p.get("score") is not None),
            None,
        )
        return GameInfo(
            platform="tuxun",
            game_id=game_id,
            status=raw.get("status") or "unknown",
            rounds=rounds,
            score=my_score,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# GeoGuessr（实验性）
# ---------------------------------------------------------------------------

class GeoGuessrError(Exception):
    """GeoGuessr API 调用失败。"""


class GeoGuessrSource(GameSource):
    """GeoGuessr 数据源（实验性）。

    使用社区熟知的非官方 API：
      GET https://www.geoguessr.com/api/v3/games/{gameId}
      → { rounds: [{panoId, lat, lng, heading, ...}], state, player: {totalScore, ...} }

    注意事项：
      * 需要 .env 配置 GEOGUESSR_COOKIE（浏览器登录后的完整 Cookie，
        支持 Cookie-Editor 导出的 JSON 数组或 JSON 文件路径）；
      * 站点有 Cloudflare 防护：Cookie 需与当前网络环境配套（含 cf_clearance），
        出现 403 时请重新从浏览器导出 Cookie 并降低请求频率；
      * rounds 中通常包含全部回合的 lat/lng（WGS84），因此进行中的对局
        也可能触发复盘模式 —— 这是官方接口返回的数据。
    """

    name = "geoguessr"
    BASE = "https://www.geoguessr.com"

    def __init__(self, cookie: str = ""):
        if not cookie or not cookie.strip():
            raise ValueError(
                "未配置 GEOGUESSR_COOKIE：请在 .env 中填入浏览器导出的 GeoGuessr Cookie"
                "（支持 Cookie-Editor JSON 文件路径或完整 Cookie 头）。"
            )
        import requests

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Referer": self.BASE,
                "Cookie": _normalize_cookie(cookie),
            }
        )

    def _get(self, path: str) -> dict:
        resp = self.session.get(f"{self.BASE}{path}", timeout=15)
        if resp.status_code == 403:
            raise GeoGuessrError(
                "被 Cloudflare 拦截（403）：请重新从浏览器导出完整 Cookie（含 cf_clearance），"
                "并确保网络环境与浏览器一致（代理设置相同）。"
            )
        if resp.status_code in (401, 402):
            raise GeoGuessrError("GeoGuessr 返回未登录（401/402），Cookie 可能已失效。")
        if resp.status_code in (404, 400):
            raise GeoGuessrError(
                "对局未找到（{}）：请确认 ID 来自 geoguessr.com/game/... 或 /challenge/... 链接。".format(resp.status_code)
            )
        if resp.status_code != 200:
            raise GeoGuessrError(f"GeoGuessr 返回异常状态码 {resp.status_code}。")
        try:
            return resp.json()
        except ValueError as exc:
            raise GeoGuessrError("GeoGuessr 返回了非 JSON 数据（可能被拦截页替换）。") from exc

    def verify(self) -> bool:
        for path in ("/api/v3/profiles/me/", "/api/v3/profile"):
            try:
                data = self._get(path)
            except GeoGuessrError:
                raise
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and (data.get("id") or data.get("playerId")):
                return True
        raise GeoGuessrError("无法验证 GeoGuessr 登录状态，请检查 GEOGUESSR_COOKIE。")

    def list_recent(self, limit: int = 10) -> List[Dict]:
        """从个人动态（feed）解析最近玩过的对局。

        GeoGuessr 没有官方"历史对局列表"API，但个人动态条目里带有 gameId。
        返回 [{"game_id": ..., "time": ..., "mode": ...}, ...]（按时间倒序）。
        """
        try:
            data = self._get("/api/v4/feed/private")
        except GeoGuessrError:
            raise
        games: List[Dict] = []
        for entry in data.get("entries") or []:
            try:
                items = json.loads(entry.get("payload") or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for item in items if isinstance(items, list) else []:
                payload = item.get("payload") or {}
                gid = payload.get("gameId")
                if gid:
                    games.append(
                        {
                            "game_id": gid,
                            "time": item.get("time") or entry.get("time"),
                            "mode": payload.get("gameMode") or "?",
                        }
                    )
        return games[:limit]

    def get_game(self, game_id: str) -> GameInfo:
        data = self._get(f"/api/v3/games/{game_id}")
        rounds: List[RoundInfo] = []
        for idx, r in enumerate(data.get("rounds") or [], start=1):
            lat, lng = r.get("lat"), r.get("lng")
            rounds.append(
                RoundInfo(
                    round_no=r.get("round") or idx,
                    pano_id=_decode_geo_pano(str(r.get("panoId") or "")),
                    source="google_pano",
                    coord_sys="wgs84",
                    heading=r.get("heading"),
                    lat=float(lat) if lat is not None else None,
                    lng=float(lng) if lng is not None else None,
                )
            )
        player = data.get("player") or {}
        total = player.get("totalScore")
        if isinstance(total, dict):  # 新版 API: {"amount": "12345", "unit": "points", ...}
            try:
                total = float(total.get("amount") or 0)
            except (TypeError, ValueError):
                total = None
        return GameInfo(
            platform="geoguessr",
            game_id=game_id,
            status=data.get("state") or "unknown",
            rounds=rounds,
            score=total,
            raw=data,
        )


# ---------------------------------------------------------------------------
# 注册表与混合检测
# ---------------------------------------------------------------------------

SOURCES: Dict[str, type] = {
    "tuxun": TuxunSource,
    "geoguessr": GeoGuessrSource,
}

# 纯 ID（无法判断来源）时的尝试顺序
_AUTO_ORDER = ["tuxun", "geoguessr"]


def make_source(name: str, cookies: Dict[str, str]) -> GameSource:
    """根据平台名构造数据源。cookies: {'tuxun': ..., 'geoguessr': ...}"""
    if name not in SOURCES:
        raise ValueError(f"未知的平台: {name}")
    return SOURCES[name](cookies.get(name, ""))


def extract_game_id(user_input: str) -> str:
    """从用户输入（ID 或完整 URL）中提取游戏 ID。"""
    text = user_input.strip()
    m = re.search(r"[?&]gameId=([A-Za-z0-9\-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"geoguessr\.com/(?:game|challenge)/([A-Za-z0-9]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"tuxun\.fun/[^\s]*?([A-Za-z0-9]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", text)
    if m:
        return m.group(1)
    return text


def detect_platforms(user_input: str) -> List[str]:
    """混合检测：判断输入属于哪些平台，返回按优先级排序的平台列表。"""
    text = user_input.strip().lower()
    if "tuxun.fun" in text:
        return ["tuxun"]
    if "geoguessr.com" in text:
        return ["geoguessr"]
    return list(_AUTO_ORDER)


def filter_platforms(requested: str, detected: List[str]) -> List[str]:
    """按用户选择的三模式过滤平台列表。"""
    if requested == "tuxun":
        return ["tuxun"]
    if requested == "geoguessr":
        return ["geoguessr"]
    return detected  # auto / 混合
