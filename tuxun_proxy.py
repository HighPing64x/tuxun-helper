#!/usr/bin/env python3
"""tuxun_proxy.py — 图寻助手 · 实时取点模式（本地代理，支持 图寻/GeoGuessr 双平台）。

原理：
  启动一个本地 mitmproxy 代理，并（可选）把它设为系统代理。当浏览器在
  tuxun.fun / geoguessr.com 加载街景时，会向 Google 请求街景元数据
  （GetMetadata），国内图源则会请求图寻自己的 get(QQ)PanoInfo 接口 ——
  这些响应里带有当前街景的经纬度。本工具拦截并解析它们，把坐标实时显示
  在内置地图上，并反查文字地址。

系统代理自适应（Clash 开启/关闭均可）：
  * 开启拦截时先记住当前系统代理；若你已在用 Clash 等代理，则自动以它为
    上级代理（级联：浏览器 -> 本工具 -> Clash -> 互联网），被墙流量照常出得去；
  * 关闭拦截 / 退出程序时恢复你原来的系统代理设置（而不是一刀切关闭）；
  * 看门狗线程防止 Clash 代理守卫改写设置，代理线程意外退出时立即自动
    恢复系统代理，避免浏览器断网（ERR_CONNECTION_CLOSED）；
  * 启动时自愈：清理上次异常退出残留的失效系统代理。

用法：
  python tuxun_proxy.py                 # 图形界面（默认）
  python tuxun_proxy.py --console       # 纯控制台输出
  python tuxun_proxy.py --tui           # TUI 后台仪表盘（端口/捕获状态/日志）
  python tuxun_proxy.py --proxy         # 启动即开启拦截（免点击）
  python tuxun_proxy.py --mirror        # 开启镜像模式（免证书免系统代理）
  python tuxun_proxy.py --port 8888     # 指定代理端口
  python tuxun_proxy.py --install-cert  # 安装 mitmproxy 根证书（首次使用必读）
  python tuxun_proxy.py --no-system-proxy  # 不自动改系统代理（浏览器手动设置）

选择页（深色分屏入口，白点圆环菜单）随控制端口常驻：
  http://127.0.0.1:18080/               # 左图寻 / 右 GeoGuessr；悬停白点=贡献者/退出/GitHub

仅供学习和技术交流使用，请遵守 tuxun.fun 服务条款。
"""

from __future__ import annotations

import argparse
import atexit
import asyncio
import json
import logging
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from urllib.parse import urlsplit
from typing import List, Optional, Tuple

from dotenv import load_dotenv

import applog
from ai_client import AIError, build_backend_from_env
from geocode import (
    DEFAULT_AMAP_JS_KEY,
    DEFAULT_AMAP_KEY,
    amap_uri_link,
    google_maps_link,
    haversine_km,
    osm_link,
    reverse_geocode,
)
from pano_images import views_for_source

try:
    from mitmproxy import http
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster
    MITMPROXY_AVAILABLE = True
except ImportError:
    MITMPROXY_AVAILABLE = False

if sys.platform == "win32":
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

WINDOWS = sys.platform == "win32" and winreg is not None

APP_VERSION = "2.0.0"   # 2.0：全网页版重构（本地仅剩 后台 CLI + 网页前端，无原生窗口）

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("tuxun.proxy")
logging.getLogger("mitmproxy").setLevel(logging.CRITICAL)  # 压掉重启/关闭时的内部噪音

BASE_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)  # PyInstaller onefile：配置/日志跟随 exe
    else os.path.dirname(os.path.abspath(__file__))
)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.jsonl")

DEFAULT_CONFIG = {
    "proxy_enabled": False,   # 程序启动时是否自动开启拦截
    "proxy_port": 8080,       # 本地代理端口
    "display_delay": 0.4,     # 捕获到坐标后的显示延迟（秒），缓解瞬间跳图
    "map_zoom": 5,            # 地图初始缩放级别
    "map_tiles": "amap",      # 瓦片源: osm（经本地代理转发，合规 UA+缓存）/ amap / arcgis；国内默认高德
    "amap_key": DEFAULT_AMAP_KEY,     # 高德 Web服务 Key（已内置默认，可在 config.json 覆盖）
    "amap_js_key": DEFAULT_AMAP_JS_KEY,  # 高德 JS Key（预留）
    "log_history": True,      # 是否把捕获点写入 history.jsonl
    "upstream_proxy": "",     # 上级代理；留空=自动跟随系统已有代理（Clash 自适应）
    "anti_decoy": True,       # 反作弊诱饵识别：距离判定 + 回合移动模式联动
    "decoy_window": 3.0,      # 距首个坐标多少秒内出现的不同坐标视为疑似诱饵
    "near_m": 150,            # 距锚点多少米内视为同一地点（确认正确而非诱饵）
    "mirror_port": 8001,      # 图寻镜像端口（--mirror 开启：免证书免系统代理，浏览器访问 127.0.0.1:端口）
    "control_port": 18080,    # 镜像悬浮窗的状态/设置 API 端口
    "mirror_enabled": True,   # 启动时自动开启镜像（2.0 起默认开：网页登录/做题都走镜像）
    "api_poll": True,         # API 直读：解析浏览器自身 solo/get 响应取真实坐标（被动、零额外请求），默认开（镜像模式无系统代理也能出答案）
    "ai_auto": False,         # AI 自动分析：新回合自动抓图分析并自动对答案（需 .env 配 AI Key）
    "cookie_declined": {"tuxun": False, "geoguessr": False},  # 自动录入 Cookie 的“否”记忆
    "name_protect": {         # NameProtect：DOM 级替换页面上显示的昵称/ID（默认关）
        "enabled": False,
        "rules": [],          # 自动学习，也可手动填 [{"match": "原名", "replace": "别名"}, ...]
    },
    "oneclock_enabled": False,  # 一键特定分数：热键触发，按分数模型反推距离并经 ws 提交（默认关）
    "oneclock_score": 3500,     # 一键目标分数（5000 满）
    "oneclock_key": "F9",       # 一键热键（游戏页面获得焦点时按下生效）
    "map_size": 0,              # 计分地图尺寸(km)：0=按回合自动（中国≈6120 / 世界≈14916）
    "overlay_enabled": True,    # 游戏镜像页是否注入图寻助手悬浮窗（关掉则不注入，页面干净）
    "open_index": True,         # 启动后自动打开选择页（http://127.0.0.1:控制端口/）
}


# ---------------------------------------------------------------------------
# 配置与系统代理
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config.update(json.load(f))
            logger.info("配置文件已读取: %s", applog.sanitize_json(config))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("config.json 读取失败，使用默认配置: %s", exc)
    if config.get("map_tiles") not in ("osm", "amap", "arcgis"):
        config["map_tiles"] = "osm"
    if not config.get("v2_mirror_migrated"):
        # 2.0 迁移：镜像成为唯一登录/做题入口，老配置一次性默认开启
        config["mirror_enabled"] = True
        config["v2_mirror_migrated"] = True
        try:
            save_config(config)
        except Exception:  # noqa: BLE001
            pass
    return config


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info("配置已保存: %s", applog.sanitize_json(config))


def set_system_proxy(enable: bool, server: str = "127.0.0.1:8080") -> bool:
    """写 Windows 系统代理（IE/WinINET 设置，Chrome/Edge 均遵循）。"""
    if not WINDOWS:
        return False
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        0, winreg.KEY_WRITE,
    )
    try:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if enable:
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
    finally:
        winreg.CloseKey(key)
    return True


def get_system_proxy() -> Tuple[bool, str]:
    """读取当前系统代理设置，返回 (是否启用, 服务器地址)。"""
    if not WINDOWS:
        return (False, "")
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_READ,
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except OSError:
            enable = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            server = ""
        winreg.CloseKey(key)
        return (bool(enable), str(server or ""))
    except OSError:
        return (False, "")


def port_listening(host: str, port: int, timeout: float = 0.6) -> bool:
    """探测端口是否有进程在监听。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def heal_stale_proxy(port: int) -> None:
    """启动自愈：清理上次异常退出残留在系统代理里的失效条目。"""
    if not WINDOWS:
        return
    enabled, server = get_system_proxy()
    our = f"127.0.0.1:{port}"
    if enabled and server == our and not port_listening("127.0.0.1", port):
        try:
            set_system_proxy(False)
            print(f"[自愈] 检测到上次异常退出残留的系统代理（{our}），已自动关闭。")
        except OSError:
            pass


def upsert_env_line(key: str, value: str, env_path: Optional[str] = None) -> None:
    """把 key=value 写入 .env（已存在则原位更新）。"""
    env_path = env_path or os.path.join(BASE_DIR, ".env")
    lines: List[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    new_line = f'{key}="{value}"'
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def install_mitm_cert() -> None:
    """把 mitmproxy 根证书安装到当前用户的受信任根证书存储。"""
    cert = os.path.join(os.path.expanduser("~"), ".mitmproxy", "mitmproxy-ca-cert.cer")
    if not WINDOWS:
        print(f"非 Windows 系统，请手动信任 mitmproxy 根证书: {cert}")
        return
    if not os.path.exists(cert):
        print("尚未找到 mitmproxy 证书。请先运行一次本程序（会自动生成证书），")
        print("然后再执行: python tuxun_proxy.py --install-cert")
        return
    print("正在安装 mitmproxy 根证书到「当前用户 → 受信任的根证书颁发机构」...")
    ret = subprocess.call(["certutil", "-user", "-addstore", "Root", cert])
    if ret == 0:
        print("证书安装完成，请重启浏览器后生效。")
    else:
        print(f"证书安装失败（certutil 返回 {ret}），请按 README 中的步骤手动导入。")


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scan_latlng(obj):
    """在任意层级的 JSON 结构中递归寻找形如 (纬度, 经度) 的相邻数值对。"""
    candidates: List[Tuple[float, float]] = []

    def walk(node) -> None:
        if len(candidates) >= 50:
            return
        if isinstance(node, list):
            if node and all(_is_number(x) for x in node) and len(node) >= 2:
                for i in range(len(node) - 1):
                    a, b = float(node[i]), float(node[i + 1])
                    if abs(a) <= 90 and abs(b) <= 180 and not (a == 0 and b == 0):
                        candidates.append((a, b))
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(obj)
    for pair in candidates:
        if pair[0] != 0 and pair[1] != 0:
            return pair
    return candidates[0] if candidates else None


def parse_google_metadata(text: Optional[str]) -> Optional[Tuple[float, float, Optional[str]]]:
    """解析 Google 街景 GetMetadata 响应，返回 (lat, lng, pano_id)，WGS84。

    pano_id 尽力提取（响应中首个形似全景 ID 的字符串），失败为 None。
    """
    if not text:
        return None
    body = text.lstrip(")]}'\r\n\t ")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None

    pano: Optional[str] = None
    try:
        for cand in (data[0][0], data[1][0][0]):
            if isinstance(cand, str) and 8 <= len(cand) <= 64 and not cand.isdigit():
                pano = cand
                break
    except (IndexError, TypeError, KeyError):
        pass

    # 已知路径：Google 地图内部 RPC 的 protobuf-JSON 结构
    try:
        node = data[1][0][5][0][1][0]
        for i, j in ((2, 3), (3, 2)):  # 兼容 lat/lng 顺序变化
            lat, lng = float(node[i]), float(node[j])
            if abs(lat) <= 90 and abs(lng) <= 180:
                return lat, lng, pano
    except (KeyError, IndexError, TypeError, ValueError):
        pass

    # 兜底：Google 改版时递归扫描
    found = _scan_latlng(data)
    return (found[0], found[1], pano) if found else None


# ---------------------------------------------------------------------------
# mitmproxy 插件
# ---------------------------------------------------------------------------

class TuxunInterceptor:
    """拦截浏览器与图寻/GeoGuessr/Google 之间的街景元数据响应。

    图寻和 GeoGuessr 在浏览器里都通过 Google 街景加载全景，因此同一套
    GetMetadata 拦截对两个平台都生效；再根据请求 Referer/Origin 区分
    来源平台，反馈时标注「图寻」或「GeoGuessr」。
    """

    def __init__(self, app: "TuxunApp"):
        self.app = app

    def request(self, flow: "http.HTTPFlow") -> None:
        """竞猜/上报请求格式记录（用于兼容性分析）。"""
        try:
            u = flow.request.pretty_url
            if "tuxun" in u and ("/game/report" in u or "/game/check" in u):
                logger.info("上报请求: %s", u[:400])
        except Exception:
            pass

    @staticmethod
    def _platform(flow: "http.HTTPFlow") -> str:
        ref = (
            flow.request.headers.get("referer")
            or flow.request.headers.get("origin")
            or ""
        ).lower()
        if "tuxun" in ref:
            return "图寻"
        if "geoguessr" in ref:
            return "GeoGuessr"
        return ""

    def websocket_message(self, flow: "http.HTTPFlow") -> None:
        """捕获图寻 websocket 对局消息（积分赛等对战模式的状态/答案走 ws 下发）。"""
        try:
            ws = flow.websocket
            if ws is None or not ws.messages:
                return
            msg = ws.messages[-1]
            text = msg.content.decode("utf-8", errors="replace")
            if not text or "heart_beat" in text:
                return
            host = flow.request.pretty_host.lower()
            if "tuxun" not in host:
                return
            direction = "上行" if msg.from_client else "下行"
            logger.info("WS消息[%s] %s", direction, text[:400])
            # 积分赛：rank 阶段直接下发本回合答案坐标 → 作为可信主点展示
            if not msg.from_client and '"status":"rank"' in text:
                m = re.search(r'"lat":\s*(-?[\d.]+)\s*,\s*"lng":\s*(-?[\d.]+)', text)
                pm = re.search(r'"panoId":"([^"]+)"', text)
                if m:
                    self.app.handle_point(
                        float(m.group(1)), float(m.group(2)), coord="wgs84",
                        source="积分赛答案揭示", pano=pm.group(1) if pm else "",
                        trusted=True,
                    )
        except Exception as exc:
            logger.debug("ws 消息捕获失败: %s", exc)

    def response(self, flow: "http.HTTPFlow") -> None:
        if flow.response is None:
            return
        url = flow.request.pretty_url
        # 被动直读：浏览器自己对局响应里就有真实坐标（零额外请求）
        if "/solo/get" in url or "/game/getContent" in url:
            try:
                m = re.search(r"[?&]gameId=([A-Za-z0-9\-]+)", url)
                if m:
                    self.app.note_game_id(m.group(1))
                if self.app.api_reader.enabled:
                    game = (json.loads(flow.response.get_text(strict=False)).get("data") or {})
                    if game.get("rounds"):
                        self.app.api_reader.note_rounds_response(game)
            except Exception as exc:
                logger.debug("对局响应直读失败: %s", exc)
        # 捕获平台 Cookie（用户同意后写入 .env）
        try:
            host = flow.request.pretty_host.lower()
            if "tuxun.fun" in host:
                ck = flow.request.headers.get("cookie", "")
                if ck:
                    self.app.note_platform_cookie("tuxun", ck)
            elif "geoguessr.com" in host:
                ck = flow.request.headers.get("cookie", "")
                if ck:
                    self.app.note_platform_cookie("geoguessr", ck)
        except Exception as exc:
            logger.debug("Cookie 捕获失败: %s", exc)
        try:
            url_lower = url.lower()
            if "google" in url_lower and "getmetadata" in url_lower:
                point = parse_google_metadata(flow.response.get_text(strict=False))
                if point:
                    lat, lng, pano = point
                    plat = self._platform(flow)
                    label = f"{plat}·谷歌街景" if plat else "谷歌街景"
                    self.app.handle_point(lat, lng, coord="wgs84", source=label, pano=pano)
                    self.app.api_reader.note_seen_pano(pano or "")
            elif "getpanoinfo" in url_lower or "getqqpanoinfo" in url_lower:
                data = json.loads(flow.response.get_text(strict=False)).get("data") or {}
                lat, lng = data.get("lat"), data.get("lng")
                if lat is not None and lng is not None:
                    pano = str(data.get("pano") or "")
                    if not pano:
                        m = re.search(r"[?&]pano=([^&]+)", url)
                        pano = m.group(1) if m else ""
                    plat = self._platform(flow) or "图寻"
                    self.app.handle_point(
                        float(lat), float(lng), coord="gcj02",
                        source=f"{plat}·腾讯街景", pano=pano,
                    )
                    self.app.api_reader.note_seen_pano(pano)
        except Exception as exc:
            logger.debug("解析响应失败 (%s): %s", url[:120], exc)


_AI_COORD_RE = re.compile(r"坐标\s*[:：]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)")

_AI_PROMPT = """你是一位顶级的图寻（GeoGuessr）专家和地理学家。
下面给你 {n} 张从同一点拍摄的街景图片，拍摄方向依次为：{labels}。
请综合所有可见线索（道路标线与行车方向、护栏/电线杆样式、路牌与招牌语言文字、
车牌、植被与地形、太阳方位、街景车特征等）进行 meta 分析，判断拍摄点位置。

请严格按照以下格式输出，不要添加任何多余的文字：

关键线索: 一句话总结最关键的判断依据
国家: [最可能的国家] ([在所属大洲的大致方位])
省/州: [最可能的省份或州] ([在所属国家的大致方位])
城市: [最可能的城市] ([在所属省/州的大致方位])
置信度: [高/中/低]
坐标: [十进制纬度], [十进制经度]

注意：坐标使用 WGS84 十进制（示例：41.9028, 12.4964），精确到小数点后 4 位。"""


_OVERLAY_SCRIPT = """(function(){
  if (window.__TUXUN_OVERLAY__) return; window.__TUXUN_OVERLAY__ = 1;
  var API = 'http://127.0.0.1:__CONTROL_PORT__';
  var st = { origin: null, current: null, answer: null, decoys: 0, candidates: 0, round_move: null,
             origin_addr: '', current_addr: '', answer_addr: '',
             settings: { anti_decoy: true, near_m: 150, display_delay: 0.4, api_poll: true, ai_auto: false,
                         oneclock_enabled: false, oneclock_score: 3500, oneclock_key: 'F9', map_size: 0 } };
  var BTN = 'background:#1a1a2e;border:1px solid #2a3350;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:11px;color:';
  var panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483647;background:rgba(12,15,22,.93);color:#cfe3ff;border:1px solid #2a3350;border-radius:8px;font:12px/1.6 "Microsoft YaHei",sans-serif;padding:10px 14px;min-width:270px;box-shadow:0 6px 24px rgba(0,0,0,.55);user-select:none';
  panel.innerHTML =
    '<div id="tx-head" style="cursor:move;color:#FFD700;font-weight:bold;user-select:none">📍 图寻助手 <span id="tx-fold" style="float:right;color:#55627e;cursor:pointer">[收起]</span></div>' +
    '<div id="tx-body">' +
    '<div>原点: <span id="tx-o" style="color:#FFD700;font-family:Consolas,monospace">-</span></div>' +
    '<div id="tx-oa" style="color:#8fa3c2;font-size:11px;margin:0 0 4px 2.6em;word-break:break-all"></div>' +
    '<div>目前: <span id="tx-c" style="color:#69db7c;font-family:Consolas,monospace">-</span> <span id="tx-cd" style="color:#8fa3c2"></span></div>' +
    '<div id="tx-ca" style="color:#8fa3c2;font-size:11px;margin:0 0 4px 2.6em;word-break:break-all"></div>' +
    '<div>答案: <span id="tx-a" style="color:#4FC3F7;font-family:Consolas,monospace">-</span></div>' +
    '<div id="tx-aa" style="color:#8fa3c2;font-size:11px;margin:0 0 4px 2.6em;word-break:break-all"></div>' +
    '<div style="color:#8fa3c2">干扰 <b id="tx-d" style="color:#d97a7a">0</b> · 候选 <b id="tx-cd2" style="color:#f0a35e">0</b> · 模式 <span id="tx-mv">-</span></div>' +
    '<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">' +
    '<button id="tx-set" style="' + BTN + '#FFD700">⚙ 设置</button>' +
    '<button id="tx-copy" style="' + BTN + '#00E5FF">复制原点</button>' +
    '<button id="tx-hide" style="' + BTN + '#9fb3d9">隐藏面板</button>' +
    '</div>' +
    '<div id="tx-tip" style="color:#55627e;font-size:11px;margin-top:2px"></div>' +
    // ---- 设置抽屉（默认隐藏）----
    '<div id="tx-setpanel" style="display:none;margin-top:8px;border-top:1px solid #223055;padding-top:8px">' +
    '<div style="color:#8fa3c2;margin-bottom:4px">— 捕获 —</div>' +
    '<label style="cursor:pointer;margin-right:10px"><input type="checkbox" id="tx-ad"> 防诱饵</label>' +
    '<label style="cursor:pointer"><input type="checkbox" id="tx-ap"> API直读</label>' +
    '<div style="margin-top:4px">判定距离 <input id="tx-near" type="number" step="10" style="width:56px;background:#0b0e18;color:#cfe3ff;border:1px solid #2a3350;border-radius:3px;padding:0 4px"> 米 · 延迟 <input id="tx-dd" type="number" step="0.1" style="width:52px;background:#0b0e18;color:#cfe3ff;border:1px solid #2a3350;border-radius:3px;padding:0 4px"> 秒</div>' +
    '<div style="color:#8fa3c2;margin:6px 0 4px">— 功能 —</div>' +
    '<label style="cursor:pointer;margin-right:10px"><input type="checkbox" id="tx-aia"> AI自动</label>' +
    '<label style="cursor:pointer"><input type="checkbox" id="tx-np"> 名称保护</label>' +
    '<div style="color:#8fa3c2;margin:6px 0 4px">— 一键特定分数 —</div>' +
    '<label style="cursor:pointer"><input type="checkbox" id="tx-oc"> 启用</label>' +
    '<div style="margin-top:4px">目标分数 <input id="tx-ocs" type="number" step="50" min="200" max="4990" style="width:64px;background:#0b0e18;color:#cfe3ff;border:1px solid #2a3350;border-radius:3px;padding:0 4px"> · 热键 <input id="tx-ock" readonly placeholder="点击录入" style="width:74px;background:#0b0e18;color:#FFD700;border:1px solid #2a3350;border-radius:3px;padding:0 4px;cursor:pointer"></div>' +
    '<div id="tx-ocinfo" style="color:#55627e;font-size:11px;margin-top:2px">在游戏中按热键 = 以该分数对应的距离自动落点提交</div>' +
    '<div style="color:#8fa3c2;margin:6px 0 4px">— 手机号区号解析 —</div>' +
    '<input id="tx-phone" placeholder="粘贴手机号，自动识别国家/地区与运营商" style="width:100%;background:#0b0e18;color:#cfe3ff;border:1px solid #2a3350;border-radius:3px;padding:3px 6px;font-size:11px">' +
    '<div id="tx-phr" style="color:#69db7c;font-size:11px;margin-top:3px;word-break:break-all"></div>' +
    '</div></div>';
  function mount(){ document.body.appendChild(panel); }
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);

  var hidden = false;
  function fmt(p){ return p ? (+p.lat).toFixed(5) + ', ' + (+p.lng).toFixed(5) : '-'; }
  function render(){
    var o = document.getElementById('tx-o'); if (!o) return;
    o.innerText = fmt(st.origin);
    document.getElementById('tx-oa').innerText = st.origin_addr || '';
    document.getElementById('tx-c').innerText = fmt(st.current);
    document.getElementById('tx-cd').innerText = (st.current && st.current.from_origin_m != null)
      ? '(' + Math.round(st.current.from_origin_m) + ' m)' : '';
    document.getElementById('tx-ca').innerText = st.current_addr || '';
    document.getElementById('tx-a').innerText = fmt(st.answer);
    document.getElementById('tx-aa').innerText = st.answer_addr || '';
    document.getElementById('tx-d').innerText = st.decoys;
    document.getElementById('tx-cd2').innerText = st.candidates;
    document.getElementById('tx-mv').innerText = st.round_move === true ? '可移动' : (st.round_move === false ? '无移动' : '-');
    var s = st.settings || {};
    var boxes = { 'tx-ad': s.anti_decoy, 'tx-ap': s.api_poll, 'tx-aia': s.ai_auto,
                  'tx-np': s.name_protect, 'tx-oc': s.oneclock_enabled };
    for (var id in boxes) { var el = document.getElementById(id); if (el && document.activeElement !== el) el.checked = !!boxes[id]; }
    function setv(id, v){ var el = document.getElementById(id); if (el && document.activeElement !== el) el.value = v; }
    setv('tx-near', s.near_m != null ? s.near_m : 150);
    setv('tx-dd', s.display_delay != null ? s.display_delay : 0.4);
    setv('tx-ocs', s.oneclock_score != null ? s.oneclock_score : 3500);
    var k = document.getElementById('tx-ock'); if (k && document.activeElement !== k) k.value = s.oneclock_key || 'F9';
    var info = document.getElementById('tx-ocinfo');
    if (info) info.innerText = st.origin
      ? ('真值就绪：按 ' + (s.oneclock_key || 'F9') + ' 即按 ' + (s.oneclock_score || 3500) + ' 分落点')
      : '等待捕获本回合真值（API直读/答案揭示后可用）';
  }
  function poll(){
    fetch(API + '/state').then(function(r){ return r.json(); }).then(function(j){ st = j; render(); syncMapMarks(); }).catch(function(){});
  }
  function save(extra){
    var body = {};
    var s = st.settings || {};
    for (var k in s) body[k] = s[k];
    for (var k2 in (extra || {})) body[k2] = extra[k2];
    fetch(API + '/settings', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function(r){ return r.json(); })
      .then(function(j){ st.settings = j.settings || st.settings; render();
        var tip = document.getElementById('tx-tip');
        if (tip) { tip.innerText = '设置已保存'; setTimeout(function(){ tip.innerText=''; }, 2000); } })
      .catch(function(){});
  }
  document.addEventListener('change', function(e){
    var map = { 'tx-ad': 'anti_decoy', 'tx-ap': 'api_poll', 'tx-aia': 'ai_auto', 'tx-oc': 'oneclock_enabled' };
    var num = { 'tx-near': 'near_m', 'tx-dd': 'display_delay', 'tx-ocs': 'oneclock_score' };
    if (map[e.target.id]) { var ex = {}; ex[map[e.target.id]] = e.target.checked; save(ex); }
    else if (num[e.target.id]) { var ex2 = {}; ex2[num[e.target.id]] = parseFloat(e.target.value) || 0; save(ex2); }
  });
  // 热键录入
  document.addEventListener('click', function(e){
    if (e.target.id === 'tx-ock') { e.target.value = '按下任意键…'; e.target.dataset.rec = '1'; }
  });
  document.addEventListener('keydown', function(e){
    var rec = document.getElementById('tx-ock');
    if (rec && rec.dataset.rec) {
      e.preventDefault(); e.stopPropagation();
      rec.dataset.rec = ''; rec.value = e.key;
      var ex = {}; ex.oneclock_key = e.key; save(ex);
      return;
    }
    oneClock(e);
  }, true);

  /* ================= 一键特定分数 =================
   * 原理：镜像页面的 ws 已被改写为本地 ws://127.0.0.1:端口。
   * 在页面脚本运行前钩住 WebSocket.prototype.send，捕获承载
   * {"scope":"tuxun"} 消息的游戏 socket；热键触发时按
   * score = 5000*exp(-10*d/size) 反推距离 d，向该 socket 发送
   * pin + confirm，与手动点击地图落点完全同构。 */
  var gameSock = null, seenSocks = [];
  try {
    var _send = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      try {
        if (seenSocks.indexOf(this) < 0) { seenSocks.push(this); if (seenSocks.length > 6) seenSocks.shift(); }
        var s = typeof data === 'string' ? data : '';
        if (s.indexOf('"scope":"tuxun"') >= 0 || s.indexOf('scope\\":\\"tuxun') >= 0) gameSock = this;
      } catch (err) {}
      return _send.apply(this, arguments);
    };
  } catch (err) {}
  function distForScore(score, sizeKm) {
    var s = Math.min(4990, Math.max(200, +score || 3500));
    return (sizeKm / 10.0) * Math.log(5000.0 / s);
  }
  function destPoint(lat, lng, distKm, bearingDeg) {
    var R = 6371.0, br = bearingDeg * Math.PI / 180, la = lat * Math.PI / 180, lo = lng * Math.PI / 180;
    var la2 = Math.asin(Math.sin(la) * Math.cos(distKm / R) + Math.cos(la) * Math.sin(distKm / R) * Math.cos(br));
    var lo2 = lo + Math.atan2(Math.sin(br) * Math.sin(distKm / R) * Math.cos(la),
                              Math.cos(distKm / R) - Math.sin(la) * Math.sin(la2));
    return { lat: la2 * 180 / Math.PI, lng: ((lo2 * 180 / Math.PI + 540) % 360) - 180 };
  }
  function wsSend(obj) {
    if (!gameSock) { // 退化：挑最近开着的 socket
      for (var i = seenSocks.length - 1; i >= 0; i--) {
        try { if (seenSocks[i].readyState === 1) { gameSock = seenSocks[i]; break; } } catch (err) {}
      }
    }
    if (!gameSock) return false;
    try { gameSock.send(JSON.stringify(obj)); return true; } catch (err) { return false; }
  }
  function oneClock(e) {
    var s = st.settings || {};
    if (!s.oneclock_enabled) return;
    if (!e || e.key !== (s.oneclock_key || 'F9')) return;
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (!st.origin) { tipMsg('一键分数：尚未捕获本回合真值'); return; }
    var coord = st.coord || 'wgs84';
    var size = +s.map_size || 0;
    if (!size) size = (coord === 'gcj02' || coord === 'bd09') ? 6120 : 14916;  // 中国图 / 世界图
    var d = distForScore(s.oneclock_score, size);
    var guess = pickLandPoint(st.origin.lat, st.origin.lng, d);
    var ok1 = wsSend({ scope: 'tuxun', data: { type: 'pin', lat: guess.lat, lng: guess.lng } });
    var ok2 = wsSend({ scope: 'tuxun', data: { type: 'confirm', lat: guess.lat, lng: guess.lng } });
    tipMsg(ok1 && ok2
      ? '一键落点 ' + Math.round(d) + ' km ≈ ' + s.oneclock_score + ' 分（' + fmt(guess) + '）'
      : '一键落点失败：未捕获游戏 ws 连接（需先进入对局）');
  }
  /* 用内置海陆掩膜（0.5° 网格）挑一个落在陆地上的落点：黄金角遍历方位，避免随机点掉进海里 */
  function pickLandPoint(lat, lng, d) {
    var lm = window.__LAND_MASK__;
    var usable = lm && lm.ready && lm.data;
    if (!usable || !(d > 0)) return destPoint(lat, lng, d, Math.random() * 360);
    var rows = 360, cols = 720;
    var a0 = Math.random() * 360;
    function landAt(lat2, lng2) {
      var row = Math.max(0, Math.min(rows - 1, Math.round((90 - lat2) * 2)));
      var col = Math.max(0, Math.min(cols - 1, Math.round((lng2 + 180) * 2)));
      return lm.data[row * cols + col] === 1;
    }
    for (var i = 0; i < 24; i++) {  // 黄金角 137.508°：方位均匀散布，不扎堆同一片海
      var p = destPoint(lat, lng, d, (a0 + i * 137.508) % 360);
      if (landAt(p.lat, p.lng)) return p;
    }
    for (var j = 0; j < 24; j++) {  // 兜底：缩到 60% 距离再试，靠岸概率更高
      var p2 = destPoint(lat, lng, d * 0.6, (a0 + j * 137.508) % 360);
      if (landAt(p2.lat, p2.lng)) return p2;
    }
    return destPoint(lat, lng, d, Math.random() * 360);  // 极端兜底：仍返回原逻辑
  }
  function tipMsg(t){ var tip = document.getElementById('tx-tip'); if (tip) { tip.innerText = t; setTimeout(function(){ tip.innerText=''; }, 3000); } }

  /* ================= 手机号区号解析 ================= */
  var CC = [["1","北美(美国/加拿大)"],["7","俄罗斯/哈萨克斯坦"],["20","埃及"],["27","南非"],["30","希腊"],["31","荷兰"],["32","比利时"],["33","法国"],["34","西班牙"],["36","匈牙利"],["39","意大利"],["40","罗马尼亚"],["41","瑞士"],["43","奥地利"],["44","英国"],["45","丹麦"],["46","瑞典"],["47","挪威"],["48","波兰"],["49","德国"],["51","秘鲁"],["52","墨西哥"],["53","古巴"],["54","阿根廷"],["55","巴西"],["56","智利"],["57","哥伦比亚"],["58","委内瑞拉"],["60","马来西亚"],["61","澳大利亚"],["62","印尼"],["63","菲律宾"],["64","新西兰"],["65","新加坡"],["66","泰国"],["81","日本"],["82","韩国"],["84","越南"],["86","中国"],["90","土耳其"],["91","印度"],["92","巴基斯坦"],["93","阿富汗"],["94","斯里兰卡"],["95","缅甸"],["98","伊朗"],["212","摩洛哥"],["213","阿尔及利亚"],["216","突尼斯"],["218","利比亚"],["220","冈比亚"],["221","塞内加尔"],["225","科特迪瓦"],["226","布基纳法索"],["230","毛里求斯"],["233","加纳"],["234","尼日利亚"],["254","肯尼亚"],["255","坦桑尼亚"],["256","乌干达"],["263","津巴布韦"],["351","葡萄牙"],["352","卢森堡"],["353","爱尔兰"],["354","冰岛"],["355","阿尔巴尼亚"],["358","芬兰"],["359","保加利亚"],["370","立陶宛"],["371","拉脱维亚"],["372","爱沙尼亚"],["373","摩尔多瓦"],["374","亚美尼亚"],["375","白俄罗斯"],["376","安道尔"],["380","乌克兰"],["381","塞尔维亚"],["385","克罗地亚"],["386","斯洛文尼亚"],["387","波黑"],["389","北马其顿"],["420","捷克"],["421","斯洛伐克"],["423","列支敦士登"],["852","中国香港"],["853","中国澳门"],["855","柬埔寨"],["856","老挝"],["880","孟加拉国"],["886","中国台湾"],["961","黎巴嫩"],["962","约旦"],["963","叙利亚"],["964","伊拉克"],["965","科威特"],["966","沙特阿拉伯"],["967","也门"],["968","阿曼"],["971","阿联酋"],["972","以色列"],["973","巴林"],["974","卡塔尔"],["975","不丹"],["976","蒙古"],["977","尼泊尔"],["992","塔吉克斯坦"],["993","土库曼斯坦"],["994","阿塞拜疆"],["995","格鲁吉亚"],["996","吉尔吉斯斯坦"],["998","乌兹别克斯坦"]];
  var phoneTimer = null;
  function parsePhone(raw){
    var d = String(raw || '').replace(/[^\d]/g, '');
    if (d.indexOf('00') === 0) d = d.slice(2);
    if (!d) return '请输入手机号';
    if (d.length === 11 && d.charAt(0) === '1') {
      // 中国手机号：第 4~7 位是地区编码（连同前 3 位 = 前 7 位号段），自动解析为省市
      if (phoneTimer) clearTimeout(phoneTimer);
      phoneTimer = setTimeout(function(){
        fetch(API + '/phone-cc?num=' + d).then(function(r){ return r.json(); }).then(function(j){
          var out = document.getElementById('tx-phr');
          if (!out) return;
          if (j && j.ok) {
            var where = j.province + (j.city && j.city !== j.province ? ' ' + j.city : '');
            out.innerText = '中国 · ' + where + '（号段 ' + j.cc + '）';
          } else {
            out.innerText = (j && j.msg) ? j.msg : '归属地解析失败';
          }
        }).catch(function(){
          var out = document.getElementById('tx-phr');
          if (out) out.innerText = '归属地解析失败（控制服务不可达）';
        });
      }, 120);
      return '解析中…';
    }
    // 非中国号码：国际区号兜底
    var cc = '';
    for (var l = 3; l >= 1; l--) {
      var p = d.slice(0, l);
      for (var i = 0; i < CC.length; i++) { if (CC[i][0] === p) { cc = CC[i]; break; } }
      if (cc) break;
    }
    if (!cc) return '未知区号（号码: +' + d.slice(0, 6) + '…）';
    var out2 = [cc[1] + ' +' + cc[0]];
    var rest = d.slice(cc[0].length);
    if (rest) out2.push('尾号 ' + (rest.length > 4 ? '…' + rest.slice(-4) : rest));
    return out2.join(' · ');
  }
  document.addEventListener('input', function(e){
    if (e.target && e.target.id === 'tx-phone') {
      var r = document.getElementById('tx-phr');
      if (r) r.innerText = parsePhone(e.target.value);
    }
  });

  document.addEventListener('click', function(e){
    if (e.target.id === 'tx-fold') {
      hidden = !hidden;
      document.getElementById('tx-body').style.display = hidden ? 'none' : 'block';
      document.getElementById('tx-fold').innerText = hidden ? '[展开]' : '[收起]';
    } else if (e.target.id === 'tx-set') {
      var p = document.getElementById('tx-setpanel');
      p.style.display = (p.style.display === 'none') ? 'block' : 'none';
    } else if (e.target.id === 'tx-copy') {
      var t = st.origin ? (+st.origin.lat).toFixed(6) + ', ' + (+st.origin.lng).toFixed(6) : '';
      if (t && navigator.clipboard) navigator.clipboard.writeText(t)
        .then(function(){ tipMsg('已复制'); });
    } else if (e.target.id === 'tx-hide') {
      panel.style.display = 'none';
      var btn = document.createElement('div');
      btn.innerText = '📍';
      btn.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483647;cursor:pointer;font-size:18px;background:rgba(12,15,22,.9);border:1px solid #2a3350;border-radius:6px;padding:4px 8px';
      btn.onclick = function(){ panel.style.display='block'; btn.remove(); };
      document.body.appendChild(btn);
    }
  });
  (function drag(){
    var sx=0, sy=0, ox=12, oy=12, on=false;
    document.addEventListener('mousemove', function(e){ if(!on) return;
      panel.style.left = (ox + e.clientX - sx) + 'px'; panel.style.bottom = 'auto';
      panel.style.top = (oy + e.clientY - sy) + 'px'; });
    document.addEventListener('mouseup', function(){ on = false; });
    document.addEventListener('DOMContentLoaded', function(){ var h=document.getElementById('tx-head');
      if (h) h.addEventListener('mousedown', function(e){
        on=true; sx=e.clientX; sy=e.clientY;
        // 以面板当前实际坐标为基点，避免每次拖动都从固定 (12,12) 起算导致跳位
        ox = panel.offsetLeft; oy = panel.offsetTop;
        e.preventDefault(); }); });
  })();
  /* ================= 游戏内地图标记（原点=金「原」 / 答案=蓝「答」） =================
   * 原理：只在悬浮窗里显示坐标不够直观，把真值点直接标到游戏地图上。
   * 地图实例可能是三种形态，统一兼容：
   *   1) 带 addMarker 的包装器（如华为 MapLibre 封装）→ 直接 addMarker
   *   2) 标准 MapLibre/Mapbox Map（getCenter+project，无 addMarker）→ 用全局 Marker 类
   *   3) 只有 project() 的任意地图对象 → 自维护 DOM 标记 + 监听 move 重投影 */
  var txMap = null, txMarks = { origin: null, answer: null };
  function txIsMapObj(o){
    return !!o && typeof o === 'object' &&
      (typeof o.addMarker === 'function' ||
       (typeof o.getCenter === 'function' && typeof o.project === 'function'));
  }
  function txFindMap(){
    if (txMap && txIsMapObj(txMap)) return txMap;
    txMap = null;
    if (window.map && txIsMapObj(window.map)) { txMap = window.map; return txMap; }
    var el = document.querySelector('.maplibregl-canvas, .maplibregl-map, .mapboxgl-canvas, .mapboxgl-map');
    if (!el) return null;
    var key = null;
    for (var k in el) { if (k.indexOf('__reactFiber$') === 0) { key = k; break; } }
    if (!key) return null;
    var seen = {}, q = [el[key]], guard = 0;
    while (q.length && guard++ < 6000) {
      var f = q.shift(); if (!f || seen[f]) continue; seen[f] = 1;
      var s = f.stateNode;
      if (s && txIsMapObj(s)) { txMap = s; return txMap; }
      if (f.return) q.push(f.return);
      if (f.child) q.push(f.child);
      for (var c = f.sibling; c; c = c.sibling) q.push(c);
    }
    return null;
  }
  function txMarkNode(color, label){
    var root = document.createElement('div');
    root.style.cssText = 'width:0;height:0;pointer-events:none;z-index:9998';
    var pin = document.createElement('div');
    pin.style.cssText = 'position:absolute;left:-12px;top:-12px;width:24px;height:24px;border-radius:50%;' +
      'background:' + color + ';border:2px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,.6);box-sizing:border-box;' +
      'color:#fff;font:700 12px/20px sans-serif;text-align:center';
    pin.innerText = label;
    root.appendChild(pin);
    return root;
  }
  function txReproject(){
    var map = txFindMap(); if (!map || typeof map.project !== 'function') return;
    for (var k in txMarks) {
      var m = txMarks[k];
      if (!m || !m._txLngLat) continue;
      try {
        var p = map.project([m._txLngLat[1], m._txLngLat[0]]);
        if (p && typeof p.x === 'number') {
          m._txNode.style.transform = 'translate(' + p.x + 'px,' + p.y + 'px)';
        }
      } catch (err) {}
    }
  }
  function txSetMark(key, lat, lng){
    var map = txFindMap(); if (!map) return;
    var color = key === 'origin' ? '#FFD700' : '#4FC3F7';
    var label = key === 'origin' ? '原' : '答';
    if (txMarks[key]) {
      try {
        var el0 = txMarks[key].getElement ? txMarks[key].getElement() : null;
        if (el0 && document.body.contains(el0)) {
          if (typeof txMarks[key].setLngLat === 'function') { txMarks[key].setLngLat([lng, lat]); return; }
          if (txMarks[key]._txLngLat) { txMarks[key]._txLngLat = [lng, lat]; txReproject(); return; }
        }
      } catch (err) {}
      try { if (txMarks[key].remove) txMarks[key].remove(); } catch (err) {}
      txMarks[key] = null;
    }
    var node = txMarkNode(color, label);
    var mk = null;
    try {
      if (typeof map.addMarker === 'function') {
        mk = map.addMarker({ element: node, lngLat: [lng, lat] });
      } else {
        var M = window.maplibregl || window.mapboxgl || null;
        if (M && M.Marker) mk = new M.Marker({ element: node }).setLngLat([lng, lat]).addTo(map);
      }
    } catch (err) { mk = null; }
    if (mk) { txMarks[key] = mk; return; }
    // 兜底：project() 定位 DOM 标记（任意带 project 的地图对象可用）
    if (typeof map.project === 'function') {
      var host = map.getContainer ? map.getContainer() :
                 (map.getCanvas ? map.getCanvas().parentElement : null);
      if (!host) return;
      var wrap = document.createElement('div');
      wrap.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;pointer-events:none;z-index:9998';
      wrap.appendChild(node);
      host.appendChild(wrap);
      txMarks[key] = { _txNode: wrap, _txLngLat: [lng, lat],
        remove: function(){ try { wrap.remove(); } catch (e) {} } };
      txReproject();
      try { map.on('move', txReproject); map.on('moveend', txReproject); } catch (e) {}
    }
  }
  function syncMapMarks(){
    var hasMap = (window.map && txIsMapObj(window.map)) ||
                 document.querySelector('.maplibregl-map, .maplibregl-canvas, .mapboxgl-map');
    if (!hasMap) return;
    if (st.origin) txSetMark('origin', +st.origin.lat, +st.origin.lng);
    if (st.answer) txSetMark('answer', +st.answer.lat, +st.answer.lng);
  }

  mount(); poll(); setInterval(poll, 1000);
})();"""

_META_CSP_RE = re.compile(
    r"<meta[^>]*http-equiv\s*=\s*[\"']content-security-policy[\"'][^>]*>", re.I
)

# ---------------------------------------------------------------------------
# NameProtect：替换页面上显示的昵称/ID（DOM 级，不改网络数据）
# ---------------------------------------------------------------------------

class NameProtect:
    """把图寻/GeoGuessr 页面上显示的昵称、数字 ID 替换为别名。

    重要教训：v1 直接改写接口 JSON 返回的 userId，客户端会把假 ID 发回
    服务器，导致「你未参赛」「已注销用户」等致命问题。v2 只对 text/html
    页面注入一段本地脚本，用 MutationObserver 替换【渲染后的文字】，
    网络数据原样透传，游戏逻辑零影响。默认关闭，GUI/config 可开启。
    """

    DOMAINS = ("tuxun.fun", "geoguessr.com")

    SCRIPT_TEMPLATE = """(function(){
  var RULES = __RULES__;
  function replaceAll(v){
    for (var i = 0; i < RULES.length; i++) {
      var r = RULES[i];
      if (!r.m || v.indexOf(r.m) < 0) continue;
      if (r.d) {
        v = v.replace(new RegExp('(^|[^0-9])' + r.m + '($|[^0-9])', 'g'), '$1' + r.r + '$2');
      } else {
        v = v.split(r.m).join(r.r);
      }
    }
    return v;
  }
  function applyText(node){
    if (!node || node.nodeType !== 3) return;
    var v = node.nodeValue;
    if (!v || !v.trim()) return;
    var nv = replaceAll(v);
    if (nv !== v) node.nodeValue = nv;
  }
  function walk(root){
    try {
      /* 叶子元素整体替换：应对 React 把昵称拆成多个文本节点的情况 */
      var els = root.getElementsByTagName('*');
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (el.children && el.children.length === 0) {
          var t = el.textContent;
          if (!t || !t.trim()) continue;
          var nt = replaceAll(t);
          if (nt !== t) el.textContent = nt;
        }
      }
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      var n; while ((n = walker.nextNode())) applyText(n);
    } catch (e) {}
  }
  function start(){
    if (!document.body) { setTimeout(start, 300); return; }
    walk(document.body);
    var mo = new MutationObserver(function(muts){
      for (var k = 0; k < muts.length; k++) {
        var mu = muts[k];
        if (mu.addedNodes) {
          for (var j = 0; j < mu.addedNodes.length; j++) {
            var n = mu.addedNodes[j];
            if (n.nodeType === 1) walk(n); else if (n.nodeType === 3) applyText(n);
          }
        }
        if (mu.type === 'characterData' && mu.target) applyText(mu.target);
      }
    });
    mo.observe(document.body, {childList:true, subtree:true, characterData:true});
  }
  start();
})();"""

    _META_CSP = re.compile(
        r"<meta[^>]*http-equiv\s*=\s*[\"']content-security-policy[\"'][^>]*>", re.I
    )

    def __init__(self, app: "TuxunApp"):
        self.app = app

    def _rules(self) -> Optional[List[dict]]:
        np = self.app.config.get("name_protect") or {}
        if not np.get("enabled"):
            return None
        rules = []
        for rule in (np.get("rules") or []):
            m = str(rule.get("match", "")).strip()
            r = str(rule.get("replace", ""))
            if m and m != r:
                rules.append({"m": m, "r": r, "d": m.isdigit()})
        return rules or None

    def response(self, flow: "http.HTTPFlow") -> None:
        rules = self._rules()
        if not rules or flow.response is None:
            return
        try:
            host = flow.request.pretty_host.lower()
        except Exception:
            return
        if not any(d in host for d in self.DOMAINS):
            return
        ctype = (flow.response.headers.get("content-type") or "").lower()
        if "text/html" not in ctype:
            return  # 只注入页面，绝不改 JSON/JS 接口数据
        try:
            text = _META_CSP_RE.sub("", flow.response.get_text(strict=False))
        except Exception:
            return
        if not text or len(text) > 5_000_000:
            return
        pos = text.lower().rfind("</body>")
        if pos < 0:
            return
        # 页面内嵌的 <meta http-equiv="CSP"> 同样会拦注入脚本，一并移除
        text = self._META_CSP.sub("", text)
        payload = json.dumps(rules, ensure_ascii=False).replace("</", "<\\/")
        injection = "<script>" + self.SCRIPT_TEMPLATE.replace("__RULES__", payload) + "</script>"
        new_text = text[:pos] + injection + text[pos:]
        try:
            # 放宽 CSP 响应头，禁止浏览器缓存旧壳页（否则脚本更新不生效）
            for h in ("content-security-policy", "content-security-policy-report-only"):
                try:
                    del flow.response.headers[h]
                except KeyError:
                    pass
            flow.response.headers["cache-control"] = "no-store"
            flow.response.set_text(new_text)
        except Exception as exc:
            logger.debug("NameProtect 注入失败: %s", exc)


# ---------------------------------------------------------------------------
# 图寻镜像（反向代理 + 页面注入）：免证书、免系统代理的本地直连入口
# ---------------------------------------------------------------------------

# 图寻 /api 直连中继的只读范围与 UA（旁路 mitmproxy 上游 TLS 指纹 403 用）。
# 仅 GET、路径以 /api/ 开头、排除登录链路——纯只读被动，符合反作弊红线。
_RELAY_API_READ_ONLY = re.compile(r"^/api/")
_RELAY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class MirrorRewrite:
    """反向代理响应改写（按平台配置）：
    * 上游站点的绝对地址改写到本地镜像（JS 里转义斜杠的写法一并处理）；
    * 可选 CDN 改道（/cdn/ 前缀路由到静态资源站，仅图寻）；
    * Set-Cookie 去掉 Domain/Secure（镜像域为 127.0.0.1 + http）；
    * 页面注入悬浮窗（原点/目前/答案 + 设置），数据来自控制 API；
    * tuxun 源同时把流量委托给 TuxunInterceptor 做坐标捕获；
    * geoguessr 源解析对局 JSON，提取每回合真实坐标。
    """

    def __init__(self, app: "TuxunApp", local_port: int, interceptor: Optional["TuxunInterceptor"],
                 control_port: int, jar: "MirrorCookieJar", *, origin: str, origin_label: str,
                 kind: str, cdn_origin: str = ""):
        self.app = app
        self.local_port = local_port
        self.interceptor = interceptor
        self.jar = jar
        self.control_port = control_port
        self.origin = origin.rstrip("/")
        self.origin_host = self.origin.split("//")[1]
        self.origin_label = origin_label
        self.kind = kind
        self.cdn_origin = cdn_origin.rstrip("/")
        self._local = f"http://127.0.0.1:{local_port}"
        self._ws_local = f"ws://127.0.0.1:{local_port}"
        self._relay_session = None  # 图寻 /api 直连中继的 requests 会话（懒创建）

    def request(self, flow: "http.HTTPFlow") -> None:
        logger.info("[RWDBG] %s request fired: %s", self.kind, flow.request.pretty_url[:60])
        # 本地字体内置：图寻页面 @font-face 引用 /fonts/BalooBhaina-*.woff2，
        # 但上游 tuxun.fun 并不提供该文件（SPA 回退返回首页 HTML，浏览器字体加载落空）。
        # 命中本地 web/fonts/ 时直接本地返回，无需改 CSS 里的 URL（相对路径本就指到本镜像）。
        if flow.request.path.startswith("/fonts/"):
            fname = flow.request.path[len("/fonts/"):].split("?")[0]
            if fname and "/" not in fname:
                data = _read_web_asset("fonts/" + fname)
                if data:
                    ctype = ("font/woff2" if fname.endswith(".woff2") else
                             "application/font-woff" if fname.endswith(".woff") else
                             "application/octet-stream")
                    flow.response = http.Response.make(
                        200, data, {"Content-Type": ctype,
                                    "Cache-Control": "public, max-age=86400"})
                    return
        # 图寻 /api 用普通 requests 直连中继（旁路 mitmproxy 上游 TLS）。
        # 实测根因（2026-09-24）：图寻 CDN 只放行真实浏览器/普通 HTTP 客户端 TLS，
        # mitmproxy 上游 ClientHello 被指纹识别，凡经本镜像转发的 /api 一律 403
        # （页面表现为『服务器走神』）；而 requests/urllib 普通 TLS 直连 /api 全 200。
        # 这里把只读的 GET /api（排除登录链路）拦截下来，亲自用 requests 取回，再塞回 flow。
        # 红线：只读被动——只 GET，绝不提交/灌包；非异步钩子里必须同步写 flow.response，
        # 不能用 flow.intercept()+线程（在 request 钩子已到钩点后再拦截不会挂住 flow）。
        if self.kind == "tuxun" and _RELAY_API_READ_ONLY.match(flow.request.path or ""):
            # 只对真实 flow 中继（FakeFlow 单测无 client_conn，直接跳过走原逻辑）。
            if bool(getattr(flow, "client_conn", None)):
                _m = (getattr(flow.request, "method", "GET") or "GET").upper()
                _p = flow.request.path or ""
                # 只中继只读 GET 且非登录链路（登录 try / QR 必须走原镜像利 Set-Cookie 落袋）。
                if _m == "GET" and "/login/" not in _p:
                    self._relay_api_get(flow)
                    return
        # 竞猜/对局请求格式记录（兼容性分析；值不含敏感信息）
        try:
            u = flow.request.pretty_url
            if self.origin_host in u and ("/game/report" in u or "/game/check" in u or "/api/v3/games" in u):
                logger.info("对局/上报请求: %s", u[:400])
        except Exception:
            pass
        # CDN 资产改道（/cdn/ 前缀 -> cdn_origin）+ 上游会话注入。
        # 注意：反向模式(reverse:)下 mitmproxy 已把 host 重写为上游 origin，
        # 请求 URL 表现为 https://tuxun.fun/...，pretty_host 恒为 origin 而非 127.0.0.1。
        # 因此不能再用 host 判断「本地流量」——本 addon 只会收到本镜像的请求，
        # 非 /cdn/ 的直接统一注入/捕获会话 cookie。
        try:
            if self.cdn_origin and flow.request.path.startswith("/cdn/"):
                flow.request.path = flow.request.path[len("/cdn"):]
                u = urlsplit(self.cdn_origin)
                flow.request.host = u.hostname
                flow.request.scheme = u.scheme
                flow.request.port = u.port or 443
            else:
                incoming = flow.request.headers.get("cookie", "")
                must = "fun_ticket=" if self.kind == "tuxun" else "session="
                if must in incoming.lower():
                    self.jar.update(incoming)
                    threading.Thread(target=self.app.note_mirror_login,
                                     args=(self.kind, incoming), daemon=True).start()
                ck = self.jar.load()
                if ck:
                    flow.request.headers["cookie"] = ck
        except Exception as exc:
            logger.debug("镜像请求改写失败: %s", exc)

    def _relay_api_get(self, flow: "http.HTTPFlow") -> None:
        """用普通 requests 直连上游，把只读 GET /api 响应取回并填回 flow。

        图寻 CDN 只放行真实浏览器/普通 HTTP 客户端 TLS，mitmproxy 上游 TLS 被指纹识别
        （凡经镜像转发的 /api 一律 403，页面『服务器走神』）；requests 普通 TLS 直连却 200。
        本方法在 request 钩子拦截后，亲自用 requests 取回，再塞回 flow 由镜像回给浏览器。
        只 GET、只读、不提交，符合反作弊红线；失败以 502 回执，不影响其余镜像流量。
        """
        try:
            method = (flow.request.method or "GET").upper()
            path = flow.request.path or "/"
            if method != "GET" or "/login/" in path:
                flow.response = http.Response.make(
                    405, b"relay: only readonly GET", {"Content-Type": "text/plain"})
                return
            cookie = self.jar.load() or ""
            headers = {
                "User-Agent": _RELAY_UA,
                "Referer": self.origin + "/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if cookie:
                headers["Cookie"] = cookie
            if self._relay_session is None:
                import requests as _requests

                self._relay_session = _requests.Session()
                self._relay_session.trust_env = False  # 图寻国内可直连，不走系统代理
            resp = self._relay_session.get(self.origin + path, headers=headers, timeout=10)
            hout = {}
            for k, v in resp.headers.items():
                if k.lower() in ("content-type", "cache-control", "location", "content-encoding"):
                    hout[k] = v
            if not hout.get("Content-Type"):
                hout["Content-Type"] = "application/json; charset=utf-8"
            flow.response = http.Response.make(resp.status_code, resp.content, hout)
        except Exception as exc:
            logger.warning("API 直连中继失败 %s: %s", flow.request.path[:60], exc)
            flow.response = http.Response.make(
                502, b"api relay failed", {"Content-Type": "text/plain"})
        finally:
            try:
                flow.resume()
            except Exception:
                pass

    def _rewrite_text(self, text: str) -> str:
        # 平台前端把 API/静态资源地址硬编码在 JS/HTML 里，全部改写到本地镜像
        text = text.replace(self.origin, self._local)
        text = text.replace(self.origin.replace("/", "\\/"), self._local.replace("/", "\\/"))
        # URL 编码变体（OAuth/跳转参数里的 redirect_uri 等）
        enc_o = self.origin.replace(":", "%3A").replace("/", "%2F")
        enc_l = self._local.replace(":", "%3A").replace("/", "%2F")
        text = text.replace(enc_o, enc_l)
        text = text.replace(enc_o.lower(), enc_l.lower())
        text = text.replace(f"wss://{self.origin_host}", self._ws_local)
        text = text.replace(f"wss:\/\/{self.origin_host}", self._ws_local)
        if self.cdn_origin:
            text = text.replace(self.cdn_origin, f"{self._local}/cdn")
        return text

    def _fix_set_cookie(self, headers) -> None:
        """去掉 Domain/Secure（镜像域为 127.0.0.1 + http），并把会话 Cookie 收进 jar。

        登录事件（图寻 fun_ticket / Geo session）额外持久化到 .env，
        让 API 直读 / AI 模式 / 重启后依然可用 —— 全网页端登录的落袋环节。
        """
        values = headers.get_all("set-cookie") if hasattr(headers, "get_all") else []
        if not values:
            return
        try:
            del headers["set-cookie"]
        except KeyError:
            pass
        import re as _re

        pairs = []
        for v in values:
            v = _re.sub(r"Domain=[^;]+;?\s*", "", v, flags=_re.I)
            v = _re.sub(r"Secure;?\s*", "", v, flags=_re.I)
            # mitmproxy 11 Headers 无 append 方法（AttributeError 曾导致登录 Set-Cookie 静默丢失），用 add
            headers.add("set-cookie", v)
            first = v.split(";", 1)[0].strip()  # 只留 name=value，丢掉 Path/HttpOnly 等属性
            if "=" in first and first not in pairs:
                pairs.append(first)
        if not pairs:
            return
        joined = "; ".join(pairs)
        self.jar.update(joined)
        must = "fun_ticket=" if self.kind == "tuxun" else "session="
        if must in joined:
            threading.Thread(target=self.app.note_mirror_login,
                             args=(self.kind, joined), daemon=True).start()

    def response(self, flow: "http.HTTPFlow") -> None:
        status_code = getattr(flow.response, "status_code", None) if flow.response else None
        logger.info("[RWDBG] %s response fired: %s %s", self.kind, status_code,
                    flow.request.pretty_url[:60])
        # 被动诊断：图寻 /api 被上游 WAF 风控 403（账号/会话黑名单，与本地 TLS/UA 无关）。
        # 只读观察既有的 403，不新增任何请求、不注入提交，符合反作弊红线。
        # 首次命中即打一条清晰可行动的日志，并置标志供 /state 与悬浮窗提示。
        if self.kind == "tuxun" and status_code == 403 and "/api/" in (flow.request.path or ""):
            if not getattr(self.app, "_tuxun_flagged", False):
                self.app._tuxun_flagged = True
                logger.warning(
                    "检测到图寻 /api 被上游风控拒绝（403）：该 Cookie 的 fun_ticket 已被图寻标记，"
                    "工具无法取真值（页面上会表现为『服务器走神』）。请在图寻官网重新登录后导出全新 Cookie，"
                    "或更换账号；若新登录仍 403 说明账号级别的限制。"
                )
        # 坐标/对局捕获与主拦截完全一致（反向模式下 host 仍为上游域名）
        if self.kind == "tuxun" and self.interceptor is not None:
            self.interceptor.response(flow)
        if flow.response is None:
            return
        # 3xx 跳转（登录回调等）：Location 指向上游域名会把用户带离镜像，改写回本地
        try:
            loc = flow.response.headers.get("location")
            if loc:
                new_loc = self._rewrite_text(loc)
                if new_loc != loc:
                    flow.response.headers["location"] = new_loc
        except Exception as exc:
            logger.debug("Location 改写失败: %s", exc)
        try:
            self._fix_set_cookie(flow.response.headers)
        except Exception as exc:
            logger.debug("Set-Cookie 改写失败: %s", exc)
        ctype = (flow.response.headers.get("content-type") or "").lower()
        if not any(t in ctype for t in ("text/html", "javascript", "json", "text/plain")):
            return
        try:
            text = flow.response.get_text(strict=False)
        except Exception as exc:
            logger.warning("镜像响应正文读取失败 (%s): %s", self.kind, exc)
            try:
                text = flow.response.content.decode("utf-8", errors="replace")
            except Exception as fallback_exc:
                logger.warning("镜像响应正文解码失败 (%s): %s", self.kind, fallback_exc)
                return
        if not text or len(text) > 8_000_000:
            return
        text = _META_CSP_RE.sub("", text)  # 页面内嵌 meta CSP 会拦注入脚本
        new = self._rewrite_text(text)
        injected = False
        if "text/html" in ctype and "</body>" in new.lower() \
                and self.app.config.get("overlay_enabled", True):
            pos = new.lower().rfind("</body>")
            injection = "<script>" + _OVERLAY_SCRIPT.replace(
                "__CONTROL_PORT__", str(self.app.config.get("control_port", 18080))
            ) + "</script>"
            new = new[:pos] + injection + new[pos:]
            injected = True
        if self.kind == "geoguessr" and "/api/v3/games" in flow.request.pretty_url:
            self._emit_geo_rounds(new)
        if new != text or injected:
            try:
                for h in ("content-security-policy", "content-security-policy-report-only"):
                    try:
                        del flow.response.headers[h]
                    except KeyError:
                        pass
                if injected:
                    flow.response.headers["cache-control"] = "no-store"
                flow.response.set_text(new)
            except Exception as exc:
                logger.debug("镜像改写失败: %s", exc)

    def _emit_geo_rounds(self, body: str) -> None:
        """GeoGuessr 对局 JSON：提取最新回合的答案坐标并上屏。"""
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return
        rounds = data.get("rounds") if isinstance(data, dict) else None
        if not rounds:
            return
        rd = rounds[-1]
        lat, lng = rd.get("lat"), rd.get("lng")
        if lat is None or lng is None:
            return
        self.app.handle_point(
            float(lat), float(lng), coord="wgs84",
            source="GeoGuessr·API直读", pano=str(rd.get("panoId") or ""),
            trusted=True,
        )

    def websocket_message(self, flow: "http.HTTPFlow") -> None:
        if self.kind == "tuxun" and self.interceptor is not None:
            self.interceptor.websocket_message(flow)
class MirrorCookieJar:
    """镜像会话的 Cookie 托管：优先使用 .env 中对应平台的 Cookie，并随服务器刷新。"""

    def __init__(self, app: "TuxunApp", env_key: str, session_file: str):
        self.app = app
        self.env_key = env_key
        self._file = os.path.join(BASE_DIR, session_file)

    def load(self) -> str:
        stored = os.getenv(self.env_key, "").strip()
        if stored:
            return stored
        if os.path.exists(self._file):
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    local = f.read().strip()
                if local:
                    return local
            except OSError:
                pass
        return stored

    def update(self, cookie_header: str) -> None:
        cookie_header = cookie_header.strip()
        if not cookie_header or cookie_header == self.load():
            return
        self.app._mirror_cookie = cookie_header
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                f.write(cookie_header)
        except OSError as exc:
            logger.debug("镜像会话保存失败: %s", exc)


_WEB_DIR = os.path.join(BASE_DIR, "web")


def _read_web_asset(name: str) -> Optional[bytes]:
    """读取 web/ 下的静态资源（PyInstaller 打包后位于 _MEIPASS/web）。"""
    base = getattr(sys, "_MEIPASS", None)
    for root in ((os.path.join(base, "web") if base else None), _WEB_DIR):
        if not root:
            continue
        path = os.path.join(root, name)
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError as exc:
                logger.warning("读取 web 资源失败 %s: %s", path, exc)
    return None


# 中国手机号归属地（前 7 位号段 -> 省/市）：惰性加载 web/phone_cc.json.gz
# 数据来源 xluohome/phonedata（仅供学习）；由 _gen_phone_cc.py 生成后即可删除生成脚本。
_PHONE_CC: Optional[dict] = None


def _phone_cc_lookup(num: str) -> Optional[dict]:
    """查询 11 位中国手机号的前 7 位号段归属地；未内置/失败返回 None。"""
    global _PHONE_CC
    if _PHONE_CC is None:
        raw = _read_web_asset("phone_cc.json.gz")
        if not raw:
            _PHONE_CC = {}
        else:
            try:
                import gzip as _gzip
                _PHONE_CC = json.loads(_gzip.decompress(raw).decode("utf-8"))
            except Exception as exc:
                logger.warning("手机号归属地数据加载失败: %s", exc)
                _PHONE_CC = {}
    return _PHONE_CC.get(num[:7])


def _graceful_exit(app: "TuxunApp") -> None:
    """网页退出入口：还原系统代理后结束进程。"""
    try:
        app.restore_proxy()
    except Exception as exc:  # noqa: BLE001
        logger.warning("还原系统代理失败: %s", exc)
    logger.info("========== 程序退出（网页请求） ==========")
    os._exit(0)


# ---------------------------------------------------------------------------
# OSM 本地瓦片代理：浏览器访问 http://127.0.0.1:控制端口/tiles/osm/{z}/{x}/{y}.png
# 为什么存在：OSM 官方瓦片按使用政策拦截了 WebView/应用类 UA（osm.wiki/Blocked 的
# 403 封锁页）。本地代理用「合规的应用 UA + 磁盘缓存」转发请求，既是政策允许的
# 轻量应用访问方式，也让瓦片可稳定通过 Clash 级联。
# ---------------------------------------------------------------------------

_TILE_CACHE_DIR = os.path.join(BASE_DIR, "cache", "tiles")
_TILE_UA = "TuxunHelper/1.0 (https://github.com/HighPing64x/tuxun-helper)"
_TILE_RE = re.compile(r"^/tiles/osm/(\d{1,2})/(\d{1,4})/(\d{1,4})(?:@2x)?\.png$")
_TILE_LAST_OK = ""   # 最近一次成功的瓦片源前缀（动态优先，避免反复等坏源超时）


def _osm_tile_fetch(z: int, x: int, y: int, app: Optional["TuxunApp"] = None) -> Optional[bytes]:
    cache = os.path.join(_TILE_CACHE_DIR, f"{z}_{x}_{y}.png")
    if os.path.isfile(cache) and os.path.getsize(cache) > 0:
        try:
            with open(cache, "rb") as f:
                return f.read()
        except OSError:
            pass
    if z > 19 or x >= (1 << z) or y >= (1 << z):
        return None
    # 上游回退链：官方瓦片服务器被部分代理出口/网络重置时，自动换 FOSSGIS 镜像。
    # 记住上次成功的源放前面，避免每次都先等坏源超时。
    global _TILE_LAST_OK
    preferred = _TILE_LAST_OK
    candidates = [f"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                  f"https://tile.openstreetmap.de/{z}/{x}/{y}.png"]
    urls = ([c for c in candidates if preferred and c.startswith(preferred)] +
            [c for c in candidates if not (preferred and c.startswith(preferred))])
    try:
        import requests

        proxies = {}
        try:
            if app is not None:
                upstream, _ = app._resolve_upstream()
                if upstream:
                    proxies = {"http": upstream, "https": upstream}
        except Exception:
            proxies = {}
        import urllib.request as _ur

        if not proxies:
            sys_proxies = _ur.getproxies()
            if sys_proxies.get("https"):
                proxies = {"http": sys_proxies["https"], "https": sys_proxies["https"]}
        resp = None
        for url in urls:
            try:
                resp = requests.get(url, headers={"User-Agent": _TILE_UA},
                                    proxies=proxies or None, timeout=10)
                if resp.status_code == 200 and resp.content:
                    _TILE_LAST_OK = url.rsplit("/", 3)[0]
                    break
                logger.debug("OSM 瓦片 %s 返回 %s", url, resp.status_code)
                resp = None
            except Exception as exc:  # noqa: BLE001
                logger.debug("OSM 瓦片 %s 失败: %s", url, exc)
                resp = None
        if resp is None:
            return None
        os.makedirs(_TILE_CACHE_DIR, exist_ok=True)
        tmp = cache + f".{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(resp.content)
        os.replace(tmp, cache)
        return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.debug("OSM 瓦片获取失败 %s: %s", url, exc)
        return None


def _pid_listening_on_port(port: int) -> list:
    """Windows: 用 netstat 找出监听该端口的 PID（无 psutil 依赖）。

    注意：netstat/tasklist 在中文 Windows 输出 GBK（cp936），必须用 mbcs 解码，
    不能用默认 text=True（UTF-8 读取会在解码线程直接崩掉）。
    """
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             timeout=8).stdout.decode("mbcs", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return sorted(p for p in pids if p)


def _pid_name(pid: int) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=8).stdout.decode("mbcs", errors="replace")
        row = out.strip().splitlines()
        if row and '","' in row[0]:
            return row[0].split('","')[0].strip('"')
    except Exception:  # noqa: BLE001
        pass
    return f"PID {pid}"


def ensure_ports_free(wanted: dict, interactive: bool = True) -> bool:
    """互斥锁：启动前检查端口占用，被占用时询问用户「退出 / 结束占用进程」。

    wanted: {端口: 用途描述}；返回 True=可以继续启动，False=用户选择退出。
    """
    conflicts = {}
    for port, usage in wanted.items():
        pids = _pid_listening_on_port(port)
        if pids:
            conflicts[port] = (usage, pids)
    if not conflicts:
        return True
    lines = ["检测到以下端口已被其他进程占用（多开互斥检查）："]
    kill_pids = set()
    for port, (usage, pids) in conflicts.items():
        names = ", ".join(f"{_pid_name(p)}({p})" for p in pids)
        lines.append(f"  端口 {port} [{usage}] <- {names}")
        kill_pids.update(pids)
    lines.append("")
    lines.append("  [1] 直接退出本程序（推荐：先关掉旧窗口）")
    lines.append("  [2] 结束占用进程并继续启动本程序")
    print("\n".join(lines))
    if not interactive:
        return False
    try:
        choice = input("请选择 (1/2，回车=1): ").strip()
    except (EOFError, OSError):
        return False
    if choice != "2":
        return False
    for pid in kill_pids:
        # 只允许结束确定占用端口的进程，避免误杀
        if pid == os.getpid():
            continue
        print(f"  结束进程 {pid} ({_pid_name(pid)}) ...")
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=8)
    time.sleep(1.2)
    still = [p for p in wanted if _pid_listening_on_port(p)]
    if still:
        print(f"端口 {still} 仍被占用（可能需要管理员权限），本次退出。")
        return False
    return True


class ControlApiHandler:
    """控制 API + 选择页主页服务（仅监听 127.0.0.1）。

    GET  /            选择页（web/index.html，深色分屏入口）
    GET  /lite.html   Lite 外置小窗（--lite 模式，pywebview 顶置显示坐标，轮询 /state、/points）
    GET  /state       悬浮窗/选择页状态轮询
    GET  /points      最近捕获点列表
    GET  /tutorial.md 打包进 exe 的使用教程（纯文本）
    POST /settings    设置更新（悬浮窗设置抽屉 / 选择页）
    POST /official-login/{platform} 官网登录 + 自动拦截 Cookie
    POST /manual-cookie            手动录入平台 Cookie
    POST /shutdown    退出程序（先还原系统代理）
    """

    _started = False  # 双镜像共用一个控制端口，避免二次绑定报错

    # 网页设置页可改的全部字段（端口类改动保存后需重启生效）
    EDITABLE_KEYS = (
        "anti_decoy", "near_m", "decoy_window", "display_delay", "api_poll", "ai_auto",
        "oneclock_enabled", "oneclock_score", "oneclock_key", "map_size",
        "map_tiles", "map_zoom", "name_protect_enabled",
        "amap_key", "amap_js_key", "overlay_enabled",
        "mirror_enabled", "open_index", "log_history",
        "proxy_port", "mirror_port", "mirror_port_geo", "control_port",
    )

    @staticmethod
    def _settings_snapshot(app: "TuxunApp") -> dict:
        np = app.config.get("name_protect") or {}
        s = {k: app.config.get(k) for k in ControlApiHandler.EDITABLE_KEYS
             if k != "name_protect_enabled"}
        s.update({
            "name_protect": bool(np.get("enabled")),
            "intercept": bool(app._proxy_set_by_us),
            "version": APP_VERSION,
        })
        return s

    @staticmethod
    def make_handler(app: "TuxunApp", mirrors: dict):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, body: dict):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_bytes(self, code, data: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                # 跨端口预检：镜像页（8001/8002）→ 控制 API（18080）POST JSON 时
                # 浏览器必发 OPTIONS，缺此处理会报 ERR_FAILED（保存设置失败）。
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Max-Age", "86400")
                self.end_headers()

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/state":
                    origin = app._round_anchor
                    with app._lock:
                        cur = app._cur_pos
                        ans = app._last_answer
                        decoys = app._round_decoys      # 只显示当回合计数
                        cands = app._round_candidates
                    pending = None
                    if app.pending_cookie:
                        pending = "图寻" if app.pending_cookie[0] == "tuxun" else "GeoGuessr"
                    self._send(200, {
                        "version": APP_VERSION,
                        "origin": {"lat": origin[0][0], "lng": origin[0][1]} if origin else None,
                        "origin_trusted": bool(origin and origin[3]),
                        "current": cur,
                        "answer": ans,
                        "coord": getattr(app, "_last_coord", "wgs84"),
                        "round_move": app._round_move,
                        "decoys": decoys,
                        "candidates": cands,
                        "origin_addr": app._origin_addr,
                        "current_addr": (cur or {}).get("address", ""),
                        "answer_addr": (ans or {}).get("address", ""),
                        "mirrors": mirrors,
                        "intercept": bool(app._proxy_set_by_us),
                        "tuxun_flagged": bool(getattr(app, "_tuxun_flagged", False)),
                        "pending_cookie": pending,
                        "cookies": {
                            "tuxun": bool(os.getenv("TUXUN_COOKIE", "").strip()),
                            "geoguessr": bool(os.getenv("GEOGUESSR_COOKIE", "").strip()),
                        },
                        "settings": ControlApiHandler._settings_snapshot(app),
                    })
                    return
                if path == "/settings":
                    self._send(200, {"status": "ok", "settings": ControlApiHandler._settings_snapshot(app)})
                    return
                if path == "/points":
                    with app._lock:
                        items = list(app._history)[-60:]
                    self._send(200, {"points": [
                        {k: r.get(k) for k in ("lat", "lng", "coord", "source", "kind",
                                               "pano", "time", "address", "from_origin_m")
                         if k in r} for r in items]})
                    return
                if path == "/phone-cc":
                    # 中国手机号归属地：取第 4~7 位（地区编码）+ 前 3 位组成前 7 位号段反查省市
                    m = re.search(r"[?&]num=(\d{7,})", self.path)
                    num = m.group(1) if m else ""
                    if len(num) == 11 and num.startswith("1"):
                        info = _phone_cc_lookup(num)
                        if info:
                            self._send(200, {"ok": True, "num": num, "cc": num[3:7],
                                             "province": info.get("p", ""),
                                             "city": info.get("c", "")})
                            return
                        self._send(200, {"ok": False, "num": num, "msg": "未收录该号段"})
                        return
                    self._send(200, {"ok": False, "num": num, "msg": "仅支持 11 位中国手机号"})
                    return
                if path in ("/", "/index.html"):
                    data = _read_web_asset("index.html")
                    if data is not None:
                        self._send_bytes(200, data, "text/html; charset=utf-8")
                    else:
                        self._send_bytes(404, "选择页资源缺失（web/index.html）".encode("utf-8"),
                                         "text/plain; charset=utf-8")
                    return
                if path == "/tutorial.md":
                    data = _read_web_asset("tutorial.md")
                    if data is not None:
                        self._send_bytes(200, data, "text/plain; charset=utf-8")
                    else:
                        self._send_bytes(404, "教程缺失".encode("utf-8"), "text/plain; charset=utf-8")
                    return
                if path == "/lite.html":
                    # Lite 外置悬浮小窗（--lite 模式，pywebview 全置顶）：轮询 /state、/points 渲染
                    data = _read_web_asset("lite.html")
                    if data is not None:
                        self._send_bytes(200, data, "text/html; charset=utf-8")
                    else:
                        self._send_bytes(404, "Lite 页面资源缺失（web/lite.html）".encode("utf-8"),
                                         "text/plain; charset=utf-8")
                    return
                if path.startswith("/vendor/"):
                    name = os.path.basename(path)  # 只允许单文件名，防目录穿越
                    data = _read_web_asset(os.path.join("vendor", name))
                    if data is not None:
                        ctype = "application/javascript" if name.endswith(".js") else (
                            "text/css" if name.endswith(".css") else "application/octet-stream")
                        self._send_bytes(200, data, ctype)
                    else:
                        self._send_bytes(404, "missing".encode("utf-8"), "text/plain; charset=utf-8")
                    return
                m = _TILE_RE.match(path)
                if m:
                    z, x, y = (int(g) for g in m.groups())
                    data = _osm_tile_fetch(z, x, y, app)
                    if data is not None:
                        self._send_bytes(200, data, "image/png")
                    else:
                        self._send_bytes(502, "tile unavailable".encode("utf-8"),
                                         "text/plain; charset=utf-8")
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if self.path.startswith("/official-login/"):
                    plat = self.path.rsplit("/", 1)[-1]
                    if plat not in ("tuxun", "geoguessr"):
                        self._send(404, {"error": "unknown platform"})
                        return
                    ok, message = app.start_official_login(plat)
                    url = "https://tuxun.fun/" if plat == "tuxun" else "https://www.geoguessr.com/"
                    self._send(200 if ok else 500, {
                        "status": "success" if ok else "error", "message": message, "url": url,
                    })
                    return
                if self.path == "/manual-cookie":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        body = json.loads(self.rfile.read(n) or b"{}")
                    except Exception as exc:
                        self._send(400, {"error": str(exc)})
                        return
                    result = app.save_manual_cookie(body.get("platform"), body.get("cookie"))
                    self._send(200 if result["status"] == "success" else 400, result)
                    return
                if self.path.startswith("/login/"):
                    # 2.0 全网页版：登录只发生在浏览器里的镜像页，返回登录页 URL
                    plat = self.path.rsplit("/", 1)[-1]
                    if plat not in ("tuxun", "geoguessr"):
                        self._send(404, {"error": "unknown platform"})
                        return
                    self._send(200, {"status": "success",
                                     "url": mirror_login_url(app.config, plat)})
                    return
                if self.path == "/cookie":
                    # 拦截模式的被动 Cookie 询问在网页端的应答入口
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        body = json.loads(self.rfile.read(n) or b"{}")
                    except Exception as exc:
                        self._send(400, {"error": str(exc)})
                        return
                    self._send(200, app.answer_cookie_prompt(bool(body.get("accept"))))
                    return
                if self.path in ("/proxy/start", "/proxy/stop"):
                    if self.path.endswith("/start"):
                        ok, message = app.enable_interception()
                        if ok:
                            app.config["proxy_enabled"] = True
                    else:
                        ok, message = app.disable_interception()
                        if ok:
                            app.config["proxy_enabled"] = False
                    save_config(app.config)
                    self._send(200, {"status": "success" if ok else "error", "message": message})
                    return
                if self.path == "/shutdown":
                    self._send(200, {"status": "exiting"})
                    logger.info("收到网页退出请求，正在还原系统代理并退出 ...")
                    threading.Thread(target=_graceful_exit, args=(app,), daemon=True).start()
                    return
                if self.path != "/settings":
                    self._send(404, {"error": "not found"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception as exc:
                    self._send(400, {"error": str(exc)})
                    return
                allowed = set(ControlApiHandler.EDITABLE_KEYS)
                for k, v in body.items():
                    if k in allowed:
                        app.config[k] = v
                    elif k == "name_protect":  # 提交的是布尔开关，规则保持不动
                        np = dict(app.config.get("name_protect") or {})
                        np["enabled"] = bool(v)
                        app.config["name_protect"] = np
                save_config(app.config)
                app.api_reader.enabled = bool(app.config.get("api_poll"))
                if app.config.get("ai_auto"):
                    app.init_ai_backend()
                logger.info("镜像悬浮窗更新设置: %s", applog.sanitize_json(body))
                self._send(200, {"status": "ok", "settings": ControlApiHandler._settings_snapshot(app)})

            def log_message(self, *a):  # 静默访问日志
                pass

        return Handler

    @staticmethod
    def start(app: "TuxunApp", port: int, mirrors: dict) -> None:
        if ControlApiHandler._started:
            return
        from http.server import ThreadingHTTPServer

        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), ControlApiHandler.make_handler(app, mirrors))
        except OSError as exc:
            logger.warning("控制端口 %d 绑定失败（可能已被本工具占用）: %s", port, exc)
            return
        ControlApiHandler._started = True
        threading.Thread(target=server.serve_forever, daemon=True, name="control-api").start()
        logger.info("控制 API / 选择页: http://127.0.0.1:%d/", port)


# ---------------------------------------------------------------------------
# 单一 mitmproxy master（镜像 reverse + 拦截 regular/upstream）+ 分发 addon
# ---------------------------------------------------------------------------

class MirrorUnifiedAddon:
    """单 master 分发 addon：按客户端连入的本地监听端口路由流量。

    背景：mitmproxy 11 同一进程同时运行多个 DumpMaster 时，各 master 共享全局
    proxyserver 注册表与事件循环，addon 钩子互相抢不到流量——实测镜像端口能
    返回上游页面但 MirrorRewrite 完全不触发（2026-09-19 冷启动复现，与「残留
    进程/启动竞态」无关）。因此本工具所有监听（镜像 reverse + 拦截 regular/
    upstream）必须合并进同一个 master，这里按 flow.client_conn.sockname 的
    本地端口把流量分发到 图寻镜像 / Geo镜像 / 拦截 三套处理链。
    """

    def __init__(self, app: "TuxunApp"):
        self.app = app
        control_port = int(app.config.get("control_port", 18080))
        self.interceptor = TuxunInterceptor(app)
        self.name_protect = NameProtect(app)
        # 镜像处理链：MIRROR_SPECS 里的每个平台一个 MirrorRewrite（端口 -> handler）
        self._handlers: dict = {}
        for kind, spec in MIRROR_SPECS.items():
            port = int(app.config.get(spec["port_key"], spec["default_port"]))
            jar = MirrorCookieJar(app, spec["env_key"], spec["session_file"])
            rw = MirrorRewrite(
                app, port, self.interceptor if kind == "tuxun" else None, control_port, jar,
                origin=spec["origin"], origin_label=spec["label"], kind=kind,
                cdn_origin=spec.get("cdn_origin", ""),
            )
            self._handlers[port] = rw
        self._proxy_port = int(app.config.get("proxy_port", 8080))
        self._intercept: object = object()  # 拦截链哨兵

    def _route(self, flow: "http.HTTPFlow"):
        """按客户端连入的本地端口取处理链；未知端口兜底走拦截链。"""
        try:
            port = flow.client_conn.sockname[1] if flow.client_conn else None
        except Exception:
            port = None
        if port is not None and port in self._handlers:
            return self._handlers[port]
        return self._intercept

    def request(self, flow: "http.HTTPFlow") -> None:
        h = self._route(flow)
        try:
            if h is self._intercept:
                self.interceptor.request(flow)
            else:
                h.request(flow)
        except Exception as exc:  # noqa: BLE001
            logger.debug("请求钩子异常: %s", exc)

    def response(self, flow: "http.HTTPFlow") -> None:
        h = self._route(flow)
        try:
            if h is self._intercept:
                self.interceptor.response(flow)
                self.name_protect.response(flow)
            else:
                h.response(flow)
        except Exception as exc:  # noqa: BLE001
            logger.debug("响应钩子异常: %s", exc)

    def websocket_message(self, flow: "http.HTTPFlow") -> None:
        h = self._route(flow)
        try:
            if h is self._intercept:
                self.interceptor.websocket_message(flow)
            else:
                h.websocket_message(flow)
        except Exception as exc:  # noqa: BLE001
            logger.debug("WS 钩子异常: %s", exc)


class ProxyServer:
    """单一 mitmproxy master 的宿主：镜像 + 拦截所有监听配置在一个 master 里。

    mitmproxy 11 同一进程只能可靠运行一个 DumpMaster（多个 master 互相踩全局
    proxyserver 注册表，addon 钩子会失效）。因此镜像端口（reverse mode）与拦截
    端口（regular/upstream mode）全部放进这一个 master 的 mode 列表，运行时通过
    Options.mode 热增删/切换 8080 槽位，支持级联上级代理。
    """

    def __init__(self, app: "TuxunApp", port: int):
        self.app = app
        self.port = port
        self.upstream: str = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._master = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _mode_list(self, upstream: str) -> List[str]:
        """完整 mode 列表：镜像 reverse（可选）+ 拦截 regular/upstream（8080 槽）。"""
        modes: List[str] = []
        if getattr(self.app, "mirror_on", True):
            for spec in MIRROR_SPECS.values():
                port = int(self.app.config.get(spec["port_key"], spec["default_port"]))
                modes.append(f"reverse:{spec['origin']}@127.0.0.1:{port}")
        listen = f"@127.0.0.1:{self.port}"
        modes.append(f"upstream:{upstream}{listen}" if upstream else f"regular{listen}")
        return modes

    def start(self, upstream: str = "") -> bool:
        """启动服务；已在运行时仅热切换/增删 mode，返回是否就绪。"""
        with self._lock:
            if self.running:
                if upstream != self.upstream:
                    self._apply_mode(upstream)
                return port_listening("127.0.0.1", self.port)
            self._launch(upstream)
            deadline = time.time() + 10
            while time.time() < deadline:
                if port_listening("127.0.0.1", self.port, timeout=0.4) and self.running:
                    return True
                if self._thread and not self._thread.is_alive():
                    return False
                time.sleep(0.15)
            return self.running and port_listening("127.0.0.1", self.port, timeout=0.4)

    def _apply_mode(self, upstream: str) -> None:
        """运行时重配整个 mode 列表（镜像常驻；8080 槽 regular/upstream 热切换）。"""
        if self._master is None or self._loop is None:
            return
        modes = self._mode_list(upstream)

        def _apply():
            try:
                self._master.options.mode = modes
                self.upstream = upstream
                logger.info("代理模式已切换: %s", " | ".join(modes))
            except Exception as exc:  # noqa: BLE001
                logger.error("切换代理模式失败: %s", exc)

        try:
            self._loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            pass

    def _launch(self, upstream: str) -> None:
        self._thread = threading.Thread(
            target=self._run, args=(upstream,), daemon=True, name="mitmproxy"
        )
        self._thread.start()
        self.upstream = upstream

    def stop(self) -> None:
        with self._lock:
            if self._master is not None and self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(self._master.shutdown)
                except RuntimeError:
                    pass  # 事件循环已关闭
                if self._thread is not None:
                    self._thread.join(timeout=8)
            self._master = None
            self._loop = None
            self._thread = None

    def _run(self, upstream: str) -> None:
        async def _main() -> None:
            opts = Options(mode=self._mode_list(upstream))
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
            master.addons.add(MirrorUnifiedAddon(self.app))
            self._master = master
            await master.run()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(_main())
        except Exception as exc:  # noqa: BLE001
            logger.error("本地代理异常退出: %s", exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# API 直读：被动读取浏览器自身的 solo/get 响应（零额外请求）
# ---------------------------------------------------------------------------

class SoloApiReader:
    """从拦截到的 solo/get 响应里直读真实坐标，绕过街景元数据诱饵。

    实测结论：诱饵只注入在客户端街景元数据（GetMetadata / getPanoInfo）里，
    solo/get 的 rounds 坐标是干净的真实答案（含进行中对局，积分赛已实测）。
    回合数据在回合内不变，因此浏览器每次请求 solo/get 的响应读一次即可；
    仅当街景元数据出现未知全景（客户端未刷新）时才主动补一次拉取（限速）。
    """

    COORD_SYS = {"google_pano": "wgs84", "qq_pano": "gcj02", "baidu_pano": "bd09"}
    FALLBACK_MIN_INTERVAL = 10.0  # 主动补拉取的最小间隔（秒）

    def __init__(self, app: "TuxunApp"):
        self.app = app
        self.enabled = False
        self._agent = None
        self._game_id: Optional[str] = None
        self._known_panos: set = set()
        self._last_fallback = 0.0

    def refresh_agent(self) -> None:
        """Cookie 更新后重建会话。"""
        self._agent = None

    def note_game(self, game_id: str) -> None:
        self._game_id = game_id

    def note_rounds_response(self, game: dict) -> None:
        """处理拦截到的对局数据（浏览器自己的 solo/get 响应，零额外请求）。"""
        rounds = game.get("rounds") or []
        for rd in rounds:
            pano = str(rd.get("panoId") or "")
            if pano:
                self._known_panos.add(pano)
        if rounds:
            rd = rounds[-1]
            if "move" in rd:
                self.app.note_round_move(rd.get("move"))
            lat, lng = rd.get("lat"), rd.get("lng")
            if lat is not None and lng is not None:
                src = rd.get("source") or "unknown"
                self.app.handle_point(
                    float(lat), float(lng),
                    coord=self.COORD_SYS.get(src, "wgs84"),
                    source="图寻API直读",
                    pano=str(rd.get("panoId") or ""),
                    trusted=True,
                )
            self.app.maybe_ai_analyze(rd)

    def note_seen_pano(self, pano: str) -> None:
        """街景元数据出现未知全景：可能客户端未刷新回合数据，补一次 solo/get。"""
        if not self.enabled or not pano or pano in self._known_panos:
            return
        if not self._game_id:
            return
        now = time.time()
        if now - self._last_fallback < self.FALLBACK_MIN_INTERVAL:
            return
        self._last_fallback = now
        self._known_panos.add(pano)  # 防止重复触发
        threading.Thread(target=self._fetch_once, args=(self._game_id,),
                         daemon=True, name="api-fallback").start()

    def _fetch_once(self, game_id: str) -> None:
        cookie = os.getenv("TUXUN_COOKIE", "").strip()
        if not cookie:
            return
        try:
            if self._agent is None:
                from tuxun_agent import TuxunAgent

                self._agent = TuxunAgent(cookie)
            game = self._agent.get_game_info(game_id)
            logger.info("API 补拉取对局状态（检测到新全景）。")
            self.note_rounds_response(game)
        except Exception as exc:
            logger.debug("API 补拉取失败 (%s): %s", game_id[:8], exc)


# ---------------------------------------------------------------------------
# 应用主体
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 网页登录（2.0 全网页版）：登录只发生在浏览器里的本地镜像页。
# 镜像捕获登录 Set-Cookie 后经 TuxunApp.note_mirror_login 落袋到 .env，
# 这里只提供登录页 URL 的推导辅助。
# ---------------------------------------------------------------------------

def mirror_login_url(config: dict, plat: str) -> str:
    port = int(config.get("mirror_port" if plat == "tuxun" else "mirror_port_geo",
                          8001 if plat == "tuxun" else 8002))
    return f"http://127.0.0.1:{port}/" + ("" if plat == "tuxun" else "login")


class TuxunApp:
    """负责捕获点分发、逆地理编码、系统代理托管与 GUI/控制台展示。"""

    def __init__(self, config: dict, console_only: bool = True):
        self.config = config
        self.console_only = True   # 2.0 全网页版：本程序只有后台 CLI，界面全在浏览器
        self.port = int(config.get("proxy_port", 8080))
        self.server = ProxyServer(self, self.port)
        self.api_reader = SoloApiReader(self)
        self._history: List[dict] = []
        self._lock = threading.Lock()
        self._last = None           # (lat, lng) 去重
        self._round_anchor = None   # (key, pano, ts, trusted) 本回合锚点（反作弊）
        self._candidates: dict = {}  # pano -> (lat, lng) 存疑候选（等待 API 校准）
        self._origin_addr = ""      # 本回合真值坐标的逆地理地址（悬浮窗「原点」小字）
        self._round_decoys = 0      # 本回合诱饵计数（新真值到达时清零）
        self._round_candidates = 0  # 本回合候选计数
        self._round_move = None     # 当前回合移动模式（True 可移动 / False 无移动 / None 未知）
        self._cur_pos = None        # {'lat','lng','from_origin_m','time'} 玩家目前位置
        self._last_answer = None    # {'lat','lng','time'} 最近一次揭示的答案
        self._last_coord = "wgs84"  # 最近一次坐标的坐标系（gcj02/bd09 => 中国图，用于一键分数自动地图尺寸）
        self._mirror_cookie = ""    # 镜像会话的图寻 Cookie（会随服务端刷新）
        self._proxy_set_by_us = False
        self._saved_proxy: Tuple[bool, str] = (False, "")  # 开启拦截前的系统代理
        self._watchdog: Optional[threading.Thread] = None
        self.ai_backend = None        # AI 自动分析后端（有 Key 且开启 ai_auto 时才加载）
        self._ai_busy = False
        self._last_ai_pano: Optional[str] = None
        self.solo_poller = None  # (兼容占位)
        self.api_reader.enabled = bool(config.get("api_poll"))
        self.cookies_known = {        # .env 里已有 Cookie 的平台不再询问
            "tuxun": bool(os.getenv("TUXUN_COOKIE", "").strip()),
            "geoguessr": bool(os.getenv("GEOGUESSR_COOKIE", "").strip()),
        }
        self._last_seen_cookie: dict = {}
        self.pending_cookie: Optional[Tuple[str, str]] = None  # (platform, cookie_header)
        self._official_login_platforms: set = set()

    # ------------------------------------------------------------------
    # Cookie 自动录入
    # ------------------------------------------------------------------
    def note_platform_cookie(self, plat: str, cookie_header: str) -> None:
        """拦截到平台请求的 Cookie 头：未录入且未拒绝时，发起“是否录入”询问。"""
        if plat in self._official_login_platforms:
            self.note_mirror_login(plat, cookie_header)
            return
        if self.cookies_known.get(plat):
            return
        if (self.config.get("cookie_declined") or {}).get(plat):
            return
        need = ("fun_ticket=",) if plat == "tuxun" else ("session=",)
        if not any(t in cookie_header for t in need):
            return
        if self._last_seen_cookie.get(plat) == cookie_header:
            return
        self._last_seen_cookie[plat] = cookie_header
        self.pending_cookie = (plat, cookie_header)
        label = "图寻" if plat == "tuxun" else "GeoGuessr"
        if self.console_only:
            print(f"\n[提示] 检测到[{label}]，是否自动录入cookie？(是/否) —— 请在下方输入")
        elif self.window is not None:
            try:
                self.window.evaluate_js(
                    f"showCookiePrompt({json.dumps(label, ensure_ascii=False)})"
                )
            except Exception as exc:
                logger.debug("Cookie 询问上屏失败: %s", exc)
        logger.info("检测到[%s] Cookie（长度 %d 字符，含凭据字段）——已请求用户确认，内容不入日志",
                    label, len(cookie))

    def answer_cookie_prompt(self, accepted) -> dict:
        """处理用户的“是/否”：是 → 写入 .env 并热更新会话；否 → 记住选择。"""
        if not self.pending_cookie:
            return {"status": "info", "message": "当前没有待处理的 Cookie。"}
        plat, cookie = self.pending_cookie
        self.pending_cookie = None
        label = "图寻" if plat == "tuxun" else "GeoGuessr"
        if accepted:
            key = "TUXUN_COOKIE" if plat == "tuxun" else "GEOGUESSR_COOKIE"
            try:
                upsert_env_line(key, cookie)
                os.environ[key] = cookie
                self.cookies_known[plat] = True
                if plat == "tuxun":
                    self.api_reader.refresh_agent()
                logger.info("Cookie 录入: 用户同意（平台=%s，已写入 .env，值不入日志）", plat)
                return {"status": "success",
                        "message": f"已自动录入 {label} Cookie（写入 .env，已即时生效）"}
            except Exception as exc:
                logger.error("Cookie 录入失败（平台=%s）: %s", plat, exc)
                return {"status": "error", "message": f"写入 .env 失败: {exc}"}
        declined = self.config.setdefault("cookie_declined", {})
        declined[plat] = True
        save_config(self.config)
        logger.info("Cookie 录入: 用户拒绝（平台=%s，已记忆不再询问）", plat)
        return {"status": "info",
                "message": f"已跳过 {label}（不再询问；可删除 config.json 的 cookie_declined 重置）"}

    def save_manual_cookie(self, plat: str, cookie_header: str) -> dict:
        """保存网页端手动输入的 Cookie，并立即更新当前进程会话。"""
        if plat not in ("tuxun", "geoguessr"):
            return {"status": "error", "message": "未知平台"}
        cookie = str(cookie_header or "").strip()
        must = "fun_ticket=" if plat == "tuxun" else "session="
        if must not in cookie.lower():
            return {"status": "error", "message": f"Cookie 中未找到 {must}"}
        key = "TUXUN_COOKIE" if plat == "tuxun" else "GEOGUESSR_COOKIE"
        try:
            upsert_env_line(key, cookie)
            os.environ[key] = cookie
            self.cookies_known[plat] = True
            if plat == "tuxun":
                self.api_reader.refresh_agent()
            logger.info("手动录入 %s Cookie，已写入 .env（值不入日志）", plat)
            return {"status": "success", "message": "Cookie 已保存并即时生效"}
        except Exception as exc:
            logger.error("手动录入 Cookie 失败（平台=%s）: %s", plat, exc)
            return {"status": "error", "message": f"写入 .env 失败: {exc}"}

    def start_official_login(self, plat: str) -> Tuple[bool, str]:
        """开启系统代理拦截，供用户在平台官网登录并自动抓取 Cookie。"""
        if plat not in ("tuxun", "geoguessr"):
            return False, "未知平台"
        ok, message = self.enable_interception()
        if not ok:
            return False, message
        self._official_login_platforms.add(plat)
        return True, f"官网登录拦截已开启：{message}"

    # ------------------------------------------------------------------
    # 内置登录窗口：打开官网让用户正常登录（图寻可扫码），自动抓 Cookie 写入 .env
    # ------------------------------------------------------------------
    def note_mirror_login(self, plat: str, cookie_header: str) -> None:
        """全网页端登录的落袋环节：镜像捕获到登录 Cookie（fun_ticket/session）后，
        写入 .env 并热更新会话。幂等：值未变化时不重复写。"""
        key = "TUXUN_COOKIE" if plat == "tuxun" else "GEOGUESSR_COOKIE"
        label = "图寻" if plat == "tuxun" else "GeoGuessr"
        current = os.getenv(key, "").strip()
        must = "fun_ticket=" if plat == "tuxun" else "session="
        if must not in cookie_header or cookie_header.strip() == current:
            return
        try:
            upsert_env_line(key, cookie_header.strip())
            os.environ[key] = cookie_header.strip()
            self.cookies_known[plat] = True
            if plat == "tuxun":
                self.api_reader.refresh_agent()
            logger.info("镜像捕获 %s 登录 Cookie，已写入 .env（值不入日志）", label)
            print(f"[登录] 检测到 {label} 登录成功，Cookie 已自动录入。")
        except Exception as exc:
            logger.error("镜像登录 Cookie 写入失败（平台=%s）: %s", plat, exc)

    # ------------------------------------------------------------------
    # AI 自动分析
    # ------------------------------------------------------------------
    def init_ai_backend(self) -> bool:
        """尝试从 .env 构建 AI 后端；成功返回 True。"""
        if self.ai_backend is not None:
            return True
        try:
            self.ai_backend = build_backend_from_env(os.environ)
            logger.info("AI 后端已就绪: %s（Key 不入日志）", self.ai_backend.model_name)
            return True
        except AIError:
            self.ai_backend = None
            logger.info("AI 后端未配置（缺少 Key/BaseURL）。")
            return False

    def maybe_ai_analyze(self, rd: dict) -> None:
        """新回合出现时触发 AI 自动分析（有后端且开启 ai_auto 才生效）。"""
        if not self.config.get("ai_auto") or self.ai_backend is None or self._ai_busy:
            return
        pano = str(rd.get("panoId") or "")
        src = rd.get("source") or ""
        if not pano or pano == self._last_ai_pano or src == "baidu_pano":
            return
        self._last_ai_pano = pano
        threading.Thread(target=self._ai_analyze, args=(dict(rd),), daemon=True,
                         name="ai-auto").start()

    def _fetch_views(self, src: str, pano: str, heading) -> List[Tuple[str, bytes]]:
        """按图源抓取方向视图（腾讯瓦片拼接 / Google 缩略图）。"""
        if src == "qq_pano":
            return views_for_source("qq_pano", pano, heading=heading or 0,
                                    count=4, include_sky=True, size=640)
        from tuxun_agent import TuxunAgent

        views = []
        for name, url in TuxunAgent.get_directional_image_urls(
            pano, count=4, include_sky=True
        ):
            views.append((name, TuxunAgent.download_image(url)))
        return views

    def _ai_analyze(self, rd: dict) -> None:
        self._ai_busy = True
        try:
            pano = str(rd.get("panoId") or "")
            src = rd.get("source") or ""
            views = self._fetch_views(src, pano, rd.get("heading"))
            prompt = _AI_PROMPT.format(n=len(views), labels="、".join(n for n, _ in views))
            print(f"[AI自动] 新回合 {pano[:16]}...，正在抓图分析（{self.ai_backend.model_name}）...")
            analysis = self.ai_backend.analyze(prompt, views)
            rec = {
                "pano": pano,
                "source": src,
                "time": datetime.now().strftime("%H:%M:%S"),
                "analysis": analysis,
            }
            m = _AI_COORD_RE.search(analysis)
            if m:
                lat, lng = float(m.group(1)), float(m.group(2))
                rec["lat"], rec["lng"] = round(lat, 6), round(lng, 6)
                tlat, tlng = rd.get("lat"), rd.get("lng")
                if tlat is not None and tlng is not None:
                    err = haversine_km(lat, lng, float(tlat), float(tlng))
                    rec["error_km"] = round(err, 1)
                    print(f"[AI自动] AI 猜测: {lat:.5f}, {lng:.5f}  距真实答案 {err:.1f} km")
                else:
                    print(f"[AI自动] AI 猜测: {lat:.5f}, {lng:.5f}")
            else:
                print("[AI自动] 未能从输出解析出坐标行。")
            print("---- AI 分析 ----")
            print(analysis)
            print("-----------------")
            if self.window is not None:
                try:
                    self.window.evaluate_js(
                        f"aiGuess({json.dumps(rec, ensure_ascii=False)})"
                    )
                except Exception as exc:
                    logger.debug("AI 结果上屏失败: %s", exc)
        except Exception as exc:  # noqa: BLE001
            print(f"[AI自动] 分析失败: {exc}")
        finally:
            self._ai_busy = False

    # ------------------------------------------------------------------
    # 捕获与分发
    # ------------------------------------------------------------------
    def _classify_locked(self, key: Tuple[float, float], pano: str, now: float) -> str:
        """分类本次捕获（按处理优先级）：

        'dup'       同坐标，或近距离坐标确认了现有锚点（无需重复展示）
        'moved'     移动回合里玩家合法移动到新位置（不是答案，静默跳过）
        'decoy'     确认诱饵（与 API 真值矛盾，或无移动回合里的远距坐标）
        'candidate' 存疑候选（真值未知时的矛盾坐标，二选一展示）
        'main'      新回合/新位置的主点

        核心判据是**距离**：诱饵要起作用必然远离真值（贴着真值放没有意义），
        而合法移动的相邻全景通常只有几十米。配合回合的 move 字段：
        无移动回合里远离锚点 = 诱饵；移动回合里远离锚点 = 玩家在移动。
        """
        if not self.config.get("anti_decoy", True):
            if self._last == key:
                return "dup"
            self._last = key
            return "main"
        anchor = self._round_anchor
        if anchor is None:
            self._round_anchor = (key, pano, now, False)
            self._last = key
            return "main"
        akey, apano, ats, atrusted = anchor
        if key == akey:
            return "dup"
        dist_m = haversine_km(key[0], key[1], akey[0], akey[1]) * 1000.0
        near_m = float(self.config.get("near_m", 150))
        if dist_m <= near_m:
            # 距离变化极小：同一地点，确认正确（诱饵不可能贴着真值放）
            if not atrusted:
                self._round_anchor = (akey, apano, ats, True)
                logger.info("附近坐标确认（距离 %.0f m），锚点已升级为可信。", dist_m)
            self._last = key
            return "dup"
        # 远距离（> near_m）：
        if pano and apano and pano == apano:
            # 同一全景却报了远处的不同坐标：投毒行为
            return "decoy" if atrusted else "candidate"
        if atrusted:
            if self._round_move is False:
                return "decoy"      # 无移动回合：远离真值的坐标必是诱饵
            if self._round_move is True:
                logger.info("移动回合：玩家新位置距答案 %.0f m，静默跳过（答案保持回合起点）。",
                            dist_m)
                return "moved"
            return "candidate"      # 移动模式未知：存疑
        if now - ats <= float(self.config.get("decoy_window", 3.0)):
            return "decoy"          # 紧跟锚点的远距坐标：疑似诱饵
        self._round_anchor = (key, pano, now, False)
        self._last = key
        return "main"

    def note_round_move(self, move) -> None:
        """记录当前回合的移动模式（来自 API 直读的回合数据）。"""
        self._round_move = self._parse_move(move)
        if move is not None:
            logger.info("回合移动模式: move=%r -> %s", move,
                        {True: "可移动", False: "无移动"}.get(self._round_move, "未知"))

    @staticmethod
    def _parse_move(move):
        """把 move 字段归一化为 True/False/None（未知）。"""
        if isinstance(move, bool):
            return move
        if isinstance(move, (int, float)):
            return bool(move)
        if isinstance(move, str):
            s = move.strip().lower()
            if s in ("true", "1", "move", "yes"):
                return True
            if s in ("false", "0", "nomove", "no"):
                return False
        return None

    def note_game_id(self, game_id: str) -> None:
        """拦截到浏览器正在玩的对局 ID → 交由 API 直读（若启用）。"""
        self.api_reader.note_game(game_id)

    def handle_point(self, lat: float, lng: float, coord: str = "wgs84",
                     source: str = "", pano: str = "", trusted: bool = False) -> None:
        key = (round(lat, 5), round(lng, 5))
        self._last_coord = coord or "wgs84"
        if trusted:
            # 可信来源（API 直读）：直接作为本回合真值锚点，并校准之前的存疑候选
            with self._lock:
                if self._last == key and self._round_anchor and self._round_anchor[3]:
                    return  # 同一答案重复揭示（rank tick 每秒一次），跳过
                self._round_anchor = (key, pano, time.time(), True)
                self._last = key
                cand = self._candidates.pop(pano, None)
                kind = "main"
            if cand is not None and cand != key:
                print(f"[校准] API 真值为 ({key[0]}, {key[1]})，"
                      f"此前候选 ({cand[0]}, {cand[1]}) 确认为诱饵。")
            elif cand is not None:
                print("[校准] 此前候选与 API 真值一致。")
        else:
            with self._lock:
                kind = self._classify_locked(key, pano, time.time())
                anchor_trusted = bool(self._round_anchor and self._round_anchor[3])
                anchor_key = self._round_anchor[0] if self._round_anchor else None
                if kind == "candidate" and pano:
                    self._candidates[pano] = key
            if kind == "decoy" and anchor_trusted and anchor_key:
                # 长期对照：街景元数据 vs API 真值的不一致率 = solo/get 是否被投毒的报警器
                err = haversine_km(key[0], key[1], anchor_key[0], anchor_key[1])
                print(f"[对照] 街景元数据坐标与 API 真值不一致（差 {err:.1f} km），已按诱饵排除。")
        if kind in ("dup",):
            return
        if kind == "main":
            # 新回合主点/真值到达：重置本回合干扰计数（悬浮窗「干扰/候选」显示当回合值）
            with self._lock:
                self._round_decoys = 0
                self._round_candidates = 0
        if kind == "moved":
            # 移动回合：更新「目前位置」（答案保持原点不动）
            origin_key = self._round_anchor[0] if self._round_anchor else key
            dist_m = haversine_km(lat, lng, origin_key[0], origin_key[1]) * 1000.0
            threading.Thread(
                target=self._deliver,
                args=(lat, lng, coord, source, 0, "moved", pano, round(dist_m)),
                daemon=True,
            ).start()
            return
        delay = random.uniform(0, max(0.0, float(self.config.get("display_delay", 0.4)))) if kind != "candidate" else 0
        threading.Thread(
            target=self._deliver, args=(lat, lng, coord, source, delay, kind, pano), daemon=True
        ).start()

    def _deliver(self, lat: float, lng: float, coord: str, source: str, delay: float,
                 kind: str = "main", pano: str = "", from_origin_m: Optional[float] = None) -> None:
        if delay > 0:
            time.sleep(delay)
        decoy = kind == "decoy"
        candidate = kind == "candidate"
        moved = kind == "moved"
        # 诱饵/候选是反作弊干扰点，不做逆地理（省请求）；主点与移动点都显示地址
        address = "" if (decoy or candidate) else reverse_geocode(
            lat, lng, amap_key=self.config.get("amap_key", ""), coord=coord
        )
        record = {
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "coord": coord,
            "source": source,
            "address": address,
            "time": datetime.now().strftime("%H:%M:%S"),
            "decoy": decoy,
            "candidate": candidate,
            "moved": moved,
            "pano": pano,
        }
        if moved and from_origin_m is not None:
            record["from_origin_m"] = from_origin_m
        with self._lock:
            self._history.append(record)
            if len(self._history) > 100:
                self._history = self._history[-100:]
        if candidate:
            logger.info("存疑候选坐标 (%s): %.6f, %.6f —— 等待 API 校准", source, lat, lng)
        elif not decoy:
            logger.info("捕获主点 (%s): %.6f, %.6f | 地址: %s",
                        source, lat, lng, address or "-")
        if self.config.get("log_history", True):
            self._append_history(record)
        if moved:
            self._cur_pos = {"lat": record["lat"], "lng": record["lng"],
                             "from_origin_m": from_origin_m, "time": record["time"],
                             "address": address}
        elif not decoy and not candidate:
            self._cur_pos = {"lat": record["lat"], "lng": record["lng"], "time": record["time"],
                             "address": address}
            self._origin_addr = address
            if source == "积分赛答案揭示":
                self._last_answer = {"lat": record["lat"], "lng": record["lng"],
                                     "time": record["time"], "address": address}
        with self._lock:
            if decoy:
                self._round_decoys += 1
            elif candidate:
                self._round_candidates += 1
        if self.console_only:
            self._print_record(record)
        elif self.window is not None:
            payload = json.dumps(record, ensure_ascii=False)
            try:
                self.window.evaluate_js(f"handleNewPoint({payload})")
            except Exception as exc:
                logger.debug("更新界面失败: %s", exc)
        if decoy:
            logger.info("检测到反作弊诱饵坐标 (%.5f, %.5f)，已保留首个正确点。", lat, lng)

    @staticmethod
    def _append_history(record: dict) -> None:
        entry = dict(record)
        entry["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("history.jsonl 写入失败: %s", exc)

    @staticmethod
    def _print_record(rec: dict) -> None:
        if rec.get("decoy"):
            print(f"[{rec['time']}] 反作弊诱饵坐标 ({rec['source']}): "
                  f"{rec['lat']:.6f}, {rec['lng']:.6f} —— 已排除，保留首个正确点")
            return
        if rec.get("candidate"):
            print(f"[{rec['time']}] 存疑候选坐标 ({rec['source']}): "
                  f"{rec['lat']:.6f}, {rec['lng']:.6f} —— 与首个坐标二选一"
                  f"（刷新场景诱饵可能先到），开启 API直读 可自动校准")
            return
        if rec.get("moved"):
            print(f"[{rec['time']}] 玩家当前位置 ({rec['source']}): "
                  f"{rec['lat']:.6f}, {rec['lng']:.6f} —— 距原点 {rec.get('from_origin_m', 0):.0f} m"
                  f"（答案保持原点）")
            return
        print(f"\n[{rec['time']}] 捕获坐标 ({rec['source']}): {rec['lat']:.6f}, {rec['lng']:.6f}")
        if rec["address"]:
            print(f"           地址: {rec['address']}")
        print(f"           OSM: {osm_link(rec['lat'], rec['lng'])}")
        print(f"           Google: {google_maps_link(rec['lat'], rec['lng'])}")
        if 3.0 <= rec["lat"] <= 55.0 and 73.0 <= rec["lng"] <= 136.0:
            print(f"           高德: {amap_uri_link(rec['lat'], rec['lng'], coord=rec['coord'])}")

    # ------------------------------------------------------------------
    # 系统代理托管（Clash 自适应 + 看门狗 + 健壮恢复）
    # ------------------------------------------------------------------
    def _resolve_upstream(self) -> Tuple[str, str]:
        """决定上级代理。返回 (upstream, 描述)。配置优先，其次自动跟随系统已有代理。"""
        explicit = str(self.config.get("upstream_proxy") or "").strip()
        if explicit:
            return explicit, explicit
        enabled, server = get_system_proxy()
        if enabled and server and server != f"127.0.0.1:{self.port}":
            return f"http://{server}", server
        return "", ""

    def enable_interception(self) -> Tuple[bool, str]:
        """开启拦截：记住原系统代理 -> 启动代理 -> 级联/直连 -> 接管系统代理。"""
        our = f"127.0.0.1:{self.port}"
        enabled, server = get_system_proxy()
        logger.info("开启拦截前系统代理状态: enable=%s server=%s", enabled, server or "-")
        if enabled and server == our and self.server.running:
            self._proxy_set_by_us = True
            return True, f"拦截已开启 ({our})"
        # 记录开启前的系统代理（退出时原样恢复；指向本工具端口的残留视为无效）
        if enabled and server != our:
            self._saved_proxy = (True, server)
        else:
            self._saved_proxy = (False, "")

        upstream, upstream_desc = self._resolve_upstream()
        if not self.server.start(upstream):
            self.restore_proxy()
            return False, f"本地代理启动失败（端口 {self.port} 可能被占用，试试 --port 8888）"

        if not set_system_proxy(True, our):
            self.restore_proxy()
            return False, "修改系统代理失败（需要 Windows 注册表权限）"
        self._proxy_set_by_us = True

        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True, name="watchdog")
            self._watchdog.start()

        self.ensure_name_protect_rules()

        msg = f"拦截已开启 ({our}"
        msg += f" → 级联 {upstream_desc})" if upstream_desc else "，直连)"
        return True, msg

    def disable_interception(self) -> Tuple[bool, str]:
        """关闭拦截：恢复开启前的系统代理（Clash 设置原样还原）。"""
        self.restore_proxy()
        return True, "拦截已关闭，系统代理已还原"

    def restore_proxy(self) -> None:
        """在任何退出路径上安全地还原系统代理。"""
        was_set = self._proxy_set_by_us
        self._proxy_set_by_us = False  # 先停看门狗
        if not was_set:
            return
        if not WINDOWS:
            return
        enabled, server = self._saved_proxy
        try:
            if enabled and server:
                set_system_proxy(True, server)
                logger.info("系统代理已还原为原设置（服务器地址不入日志）。")
                print("系统代理已还原为你原来的代理设置。")
            else:
                set_system_proxy(False)
                logger.info("系统代理已还原（关闭）。")
                print("系统代理已还原（关闭）。")
        except OSError as exc:
            logger.error("系统代理还原失败，请到 Windows 设置中手动处理: %s", exc)

    def _watchdog_loop(self) -> None:
        """看门狗：夺回被代理守卫改写的系统代理；代理线程死亡则立即自保恢复。"""
        our = f"127.0.0.1:{self.port}"
        while self._proxy_set_by_us:
            time.sleep(2)
            if not self._proxy_set_by_us:
                break
            # 1) 代理服务线程意外死亡 -> 立即还原，避免浏览器指向死端口
            if not self.server.running:
                print("[看门狗] 本地代理线程异常退出，已自动还原系统代理。")
                self.restore_proxy()
                self._notify("本地代理异常退出，已自动还原系统代理！请查看控制台日志。")
                break
            # 2) 系统代理被其他程序（如 Clash 守卫）改写 -> 重新接管
            enabled, server = get_system_proxy()
            if not enabled or server != our:
                try:
                    set_system_proxy(True, our)
                    logger.info("检测到系统代理被改写，已重新接管 (%s)", our)
                except OSError:
                    pass

    def _notify(self, message: str) -> None:
        if self.console_only:
            print(f"[!] {message}")
        elif self.window is not None:
            try:
                self.window.evaluate_js(
                    f"status({json.dumps(message, ensure_ascii=False)}, 10000)"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # NameProtect 自动学习
    # ------------------------------------------------------------------
    def ensure_name_protect_rules(self) -> None:
        """enabled 且 rules 为空时自动学习当前账号的昵称/ID 并生成随机别名（后台线程）。"""
        np = self.config.get("name_protect")
        if not isinstance(np, dict) or not np.get("enabled") or np.get("rules"):
            return

        def _rand_name() -> str:
            return "Player_" + "".join(random.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=4))

        def learn() -> None:
            rules: List[dict] = []
            # 图寻：历史对局拿 userId，最近 solo 局的 players 里拿昵称
            try:
                from tuxun_agent import TuxunAgent

                cookie = os.getenv("TUXUN_COOKIE", "").strip()
                if cookie:
                    agent = TuxunAgent(cookie)
                    uid = None
                    games = agent.get_history(1, 10)
                    for g in games:
                        if g.get("userId"):
                            uid = str(g["userId"])
                            break
                    name = None
                    for g in games:
                        if name or g.get("type") != "solo":
                            continue
                        for p in (agent.get_game_info(g["gameId"]).get("players") or []):
                            if str(p.get("userId")) == uid and p.get("userName"):
                                name = str(p["userName"])
                                break
                    if uid and name and name != "已注销用户":
                        rules.append({"match": name, "replace": _rand_name()})
                        rules.append({"match": uid,
                                      "replace": str(random.randint(2000000, 9876543))})
            except Exception as exc:
                logger.debug("NameProtect 学习(图寻)失败: %s", exc)
            # GeoGuessr：profiles/me 直接给昵称和 ID
            try:
                from game_sources import make_source

                src = make_source("geoguessr", {"geoguessr": os.getenv("GEOGUESSR_COOKIE", "")})
                prof = src._get("/api/v3/profiles/me/")
                gid, nick = prof.get("id"), prof.get("nick")
                if gid and nick:
                    rules.append({"match": str(nick), "replace": _rand_name()})
                    rules.append({"match": str(gid),
                                  "replace": "".join(random.choices("0123456789abcdef", k=24))})
            except Exception as exc:
                logger.debug("NameProtect 学习(GeoGuessr)失败: %s", exc)

            if rules:
                np["rules"] = rules
                self.config["name_protect"] = np
                save_config(self.config)
                logger.info("NameProtect 已自动学习 %d 条规则（内容不入日志）。", len(rules))
                summary = ", ".join(f"{r['match']} → {r['replace']}" for r in rules)
                print(f"[NameProtect] 已自动学习并启用: {summary}")
            else:
                logger.info("NameProtect 自动学习失败：未获得昵称/ID。")
                print("[NameProtect] 未能自动学习到昵称/ID。"
                      "可在 config.json 的 name_protect.rules 手动配置，"
                      '例如 {"match": "你的昵称", "replace": "别名"}。')

        threading.Thread(target=learn, daemon=True, name="nameprotect-learn").start()

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def print_banner(app: TuxunApp, auto_proxy: bool, mirror_on: bool = False) -> None:
    upstream, upstream_desc = app._resolve_upstream()
    cport = int(app.config.get("control_port", 18080))
    print("=" * 56)
    print(f"  图寻助手 v{APP_VERSION} · 后台服务（网页前端见下）")
    print(f"  拦截代理端口: 127.0.0.1:{app.port}（仅代理协议，浏览器不要直接打开）")
    if WINDOWS:
        print(f"  系统代理: {'已接管' if auto_proxy else '未接管（点击界面/查看下方提示）'}")
    else:
        print(f"  非 Windows：请在浏览器中手动设置 HTTP 代理为 127.0.0.1:{app.port}")
    print(f"  上级代理: {upstream_desc + '（自动级联）' if upstream_desc else '直连'}")
    print(f"  选择页/网页控制台: http://127.0.0.1:{cport}/")
    if mirror_on:
        mport = int(app.config.get("mirror_port", 8001))
        gport = int(app.config.get("mirror_port_geo", 8002))
        print(f"  图寻镜像: http://127.0.0.1:{mport}（免证书，浏览器直接访问做题）")
        print(f"  Geo镜像: http://127.0.0.1:{gport}（未登录也可进，页面内登录自动录入）")
    print("  平台识别: 按请求来源自动标注 图寻 / GeoGuessr")
    print("  证书: 首次使用请安装（--install-cert / 见 README）")
    print("=" * 56)


def install_signal_handlers(app: TuxunApp) -> None:
    """尽力保证任何退出路径都会还原系统代理。"""
    atexit.register(app.restore_proxy)

    def _graceful(signum, frame):
        app.restore_proxy()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _graceful)
        signal.signal(signal.SIGTERM, _graceful)
    except (ValueError, OSError):
        pass

    if sys.platform == "win32":
        # 捕获控制台窗口被直接关闭（CTRL_CLOSE_EVENT 等不触发 atexit 的情况）
        import ctypes

        def _ctrl_handler(event_type):
            app.restore_proxy()
            return False  # 继续默认终止流程

        _HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        _handler_ref = _HANDLER(_ctrl_handler)  # 防止被 GC
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler_ref, True)
        # 保存引用防止回收
        app._ctrl_handler_ref = _handler_ref


MIRROR_SPECS = {
    "tuxun": {"origin": "https://tuxun.fun", "label": "图寻", "kind": "tuxun",
              "env_key": "TUXUN_COOKIE", "session_file": "_mirror_session_tuxun.txt",
              "cdn_origin": "https://b68res.daai.fun", "port_key": "mirror_port",
              "default_port": 8001},
    "geoguessr": {"origin": "https://www.geoguessr.com", "label": "GeoGuessr", "kind": "geoguessr",
                  "env_key": "GEOGUESSR_COOKIE", "session_file": "_mirror_session_geo.txt",
                  "cdn_origin": "", "port_key": "mirror_port_geo", "default_port": 8002},
}


def _serve_loop(app: "TuxunApp") -> None:
    """Lite 模式的服务循环（搬进后台 daemon 线程运行）。

    主进程需把主线程让给 pywebview 的 GUI 事件循环（窗口关闭后 program
    才正常收尾），故把原来的 pending_cookie 提示循环抽出来在线程里跑。
    """
    try:
        while True:
            if app.pending_cookie:
                plat, _ck = app.pending_cookie
                label = "图寻" if plat == "tuxun" else "GeoGuessr"
                ans = input(f"检测到[{label}]，是否自动录入cookie？(是/否): ").strip().lower()
                result = app.answer_cookie_prompt(ans in ("是", "y", "yes", "1"))
                print(f"  -> {result['message']}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在退出 ...")


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(errors="replace", line_buffering=True)
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description=f"图寻助手 v{APP_VERSION} · 后台服务（网页前端 http://127.0.0.1:18080/）")
    parser.add_argument("--console", action="store_true", help="（兼容保留）本版本始终为控制台+网页")
    parser.add_argument("--tui", action="store_true", help="TUI 后台仪表盘（端口/捕获状态/日志尾部）")
    parser.add_argument("--port", type=int, help="本地代理端口（默认读取 config.json，初始 8080）")
    parser.add_argument("--proxy", action="store_true", help="启动后立即开启拦截并接管系统代理")
    parser.add_argument("--mirror", action="store_true", help="开启镜像（默认已开启）")
    parser.add_argument("--no-mirror", action="store_true", help="关闭镜像启动（默认镜像为开）")
    parser.add_argument("--login", choices=["tuxun", "geoguessr"], metavar="平台",
                        help="在系统浏览器打开对应镜像登录页（网页登录），Cookie 自动录入")
    parser.add_argument("--install-cert", action="store_true", help="安装 mitmproxy 根证书后退出")
    parser.add_argument("--lite", action="store_true",
                        help="Lite 模式：不注入页面悬浮窗，改用 pywebview 外置小窗显示坐标")
    parser.add_argument("--no-system-proxy", action="store_true", help="不自动改系统代理（浏览器手动设置）")
    args = parser.parse_args()

    if args.install_cert:
        install_mitm_cert()
        return

    if not MITMPROXY_AVAILABLE:
        print("错误：缺少 mitmproxy 库，请先执行：")
        print("  pip install mitmproxy")
        print("（国内可使用镜像: pip install mitmproxy -i https://pypi.tuna.tsinghua.edu.cn/simple）")
        sys.exit(1)

    config = load_config()
    if args.lite:
        # Lite 模式：只读轮询控制 API 渲染外置小窗，无需往游戏页面注入覆盖层，
        # 故本次进程关闭 overlay 注入（仅改运行态 config 对象，不写回 config.json）。
        config["overlay_enabled"] = False
    if args.port:
        config["proxy_port"] = args.port
    load_dotenv(os.path.join(BASE_DIR, ".env"))  # NameProtect 学习需要读取 Cookie
    log_file = applog.setup(filename="proxy")
    logger.info("========== 实时取点模式启动 ==========")
    logger.info("Python %s | %s | 日志文件: %s", sys.version.split()[0], sys.platform, log_file)

    mirror_on = not args.no_mirror and bool(args.mirror or config.get("mirror_enabled", True))

    # ---- 互斥锁：端口占用检查（多开会冲突/互相写配置）----
    wanted_ports = {int(config.get("proxy_port", 8080)): "本地代理",
                    int(config.get("control_port", 18080)): "控制API/选择页"}
    if mirror_on:
        wanted_ports[int(config.get("mirror_port", 8001))] = "图寻镜像"
        wanted_ports[int(config.get("mirror_port_geo", 8002))] = "GeoGuessr 镜像"
    if not ensure_ports_free(wanted_ports):
        logger.info("因端口占用选择退出（互斥检查）。")
        return

    app = TuxunApp(config)
    if config.get("ai_auto") and app.init_ai_backend():
        print(f"[AI自动] 已就绪（{app.ai_backend.model_name}），"
              "检测到新回合会自动抓图分析并对答案。")
    elif config.get("ai_auto"):
        print("[AI自动] 已开启但未配置 AI Key（.env），该功能暂不生效。")

    # 任何退出路径都还原系统代理
    install_signal_handlers(app)
    # 启动自愈：清理上次异常退出残留的失效系统代理
    heal_stale_proxy(app.port)

    # 镜像模式：免证书免系统代理的本地直连入口（悬浮窗注入到页面）
    app.mirror_on = mirror_on   # 供 ProxyServer._mode_list 决定是否挂 reverse 监听
    mirrors: dict = {}
    # 控制 API + 选择页常驻（未开镜像也能打开主页看状态/教程/退出）
    ControlApiHandler.start(app, int(config.get("control_port", 18080)), mirrors)
    if mirror_on:
        # 未登录也启动镜像：登录 Cookie 会在请求/响应中被自动捕获（全网页端登录链路）
        for spec in MIRROR_SPECS.values():
            mirrors[spec["kind"]] = int(config.get(spec["port_key"], spec["default_port"]))

    # 单一 mitmproxy master 承载 镜像(reverse 8001/8002) + 拦截(8080)：
    # mitmproxy 11 同进程多 DumpMaster 会互相踩全局 proxyserver 注册表，导致
    # 镜像 addon 钩子失效（2026-09-19 实测根因），因此所有监听合并为一个 master。
    upstream, _ = app._resolve_upstream()
    app.server.start(upstream)
    if mirror_on:
        deadline = time.time() + 8
        while time.time() < deadline and not all(port_listening("127.0.0.1", p) for p in mirrors.values()):
            time.sleep(0.15)
        started = [f"{k}=http://127.0.0.1:{p}" for k, p in mirrors.items()
                   if port_listening("127.0.0.1", p)]
        if started:
            logger.info("镜像已就绪: %s | 选择页: http://127.0.0.1:%d/", " | ".join(started),
                        int(config.get("control_port", 18080)))
        else:
            logger.warning("没有镜像端口启动成功。")
    print(f"拦截代理端口 127.0.0.1:{app.port} 已启动（仅代理协议，浏览器不要直接打开这个端口），等待捕获街景坐标 ...\n")

    # 自动打开选择页（深色分屏入口）。Lite 模式改为外置小窗，不自动弹选择页。
    if not args.lite and config.get("open_index", True):
        try:
            webbrowser.open(f"http://127.0.0.1:{int(config.get('control_port', 18080))}/")
        except Exception as exc:  # noqa: BLE001
            logger.debug("打开选择页失败: %s", exc)

    # --login：系统浏览器直接打开镜像登录页（网页登录，Cookie 自动录入）
    if args.login:
        try:
            ok, message = app.start_official_login(args.login)
            url = "https://tuxun.fun/" if args.login == "tuxun" else "https://www.geoguessr.com/"
            webbrowser.open(url)
            print(f"[登录] {message}；已在系统浏览器打开 {url} —— 登录后 Cookie 自动录入。")
        except Exception as exc:  # noqa: BLE001
            logger.debug("打开登录页失败: %s", exc)

    auto_proxy = False
    if not args.no_system_proxy and (args.proxy or config.get("proxy_enabled")):
        ok, message = app.enable_interception()
        auto_proxy = ok
        print(f"[{ 'OK' if ok else '失败' }] {message}")
        logger.info("启动时自动开启拦截: %s -> %s", ok, message)
    elif not WINDOWS and not args.no_system_proxy:
        print(f"[提示] 请在浏览器中手动设置 HTTP 代理为 127.0.0.1:{app.port}")

    print_banner(app, auto_proxy, mirror_on)

    if args.tui:
        try:
            import tuxun_tui
            tuxun_tui.run_tui(app, mirrors, log_file)
        except KeyboardInterrupt:
            print("\n正在退出 ...")
    elif args.lite:
        # Lite 外置小窗：pywebview 必须在"主线程"跑 GUI 事件循环，因此把
        # 服务循环(含 pending_cookie 提示)放进后台 daemon 线程，再由主线程
        # 打开顶置小窗并阻塞等待。窗口关闭后 webview.start() 返回，正常收尾。
        threading.Thread(target=_serve_loop, args=(app,),
                         name="lite-serve", daemon=True).start()
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            print("缺少 pywebview，无法打开 Lite 小窗（请先 pip install pywebview）：", exc)
        else:
            url = f"http://127.0.0.1:{int(config.get('control_port', 18080))}/lite.html"
            try:
                webview.create_window("图寻助手 Lite", url, width=420, height=360,
                                      resizable=True, on_top=True, min_size=(340, 300))
                webview.start(debug=False)  # 阻塞主线程；窗口关闭后再收尾退出
            except Exception as exc:  # noqa: BLE001
                logger.exception("Lite 窗口启动失败: %s", exc)
    else:
        try:
            while True:
                if app.pending_cookie:
                    plat, _ck = app.pending_cookie
                    label = "图寻" if plat == "tuxun" else "GeoGuessr"
                    ans = input(f"检测到[{label}]，是否自动录入cookie？(是/否): ").strip().lower()
                    result = app.answer_cookie_prompt(ans in ("是", "y", "yes", "1"))
                    print(f"  -> {result['message']}")
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在退出 ...")
    logger.info("========== 程序退出 ==========")


if __name__ == "__main__":
    main()
