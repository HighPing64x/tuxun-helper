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
  python tuxun_proxy.py --proxy         # 启动即开启拦截（免点击）
  python tuxun_proxy.py --port 8888     # 指定代理端口
  python tuxun_proxy.py --install-cert  # 安装 mitmproxy 根证书（首次使用必读）
  python tuxun_proxy.py --no-system-proxy  # 不自动改系统代理（浏览器手动设置）

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

try:
    import webview
    WEBVIEW_AVAILABLE = True
except ImportError:
    WEBVIEW_AVAILABLE = False

if sys.platform == "win32":
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

WINDOWS = sys.platform == "win32" and winreg is not None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("tuxun.proxy")
logging.getLogger("mitmproxy").setLevel(logging.CRITICAL)  # 压掉重启/关闭时的内部噪音
logging.getLogger("pywebview").setLevel(logging.CRITICAL)  # 压掉 pywebview 6.x 属性枚举刷屏

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
    "map_tiles": "osm",       # 瓦片源: osm / amap / arcgis
    "amap_key": DEFAULT_AMAP_KEY,     # 高德 Web服务 Key（已内置默认，可在 config.json 覆盖）
    "amap_js_key": DEFAULT_AMAP_JS_KEY,  # 高德 JS Key（预留）
    "log_history": True,      # 是否把捕获点写入 history.jsonl
    "upstream_proxy": "",     # 上级代理；留空=自动跟随系统已有代理（Clash 自适应）
    "anti_decoy": True,       # 反作弊诱饵识别：距离判定 + 回合移动模式联动
    "decoy_window": 3.0,      # 距首个坐标多少秒内出现的不同坐标视为疑似诱饵
    "near_m": 150,            # 距锚点多少米内视为同一地点（确认正确而非诱饵）
    "mirror_port": 8001,      # 图寻镜像端口（--mirror 开启：免证书免系统代理，浏览器访问 127.0.0.1:端口）
    "control_port": 18080,    # 镜像悬浮窗的状态/设置 API 端口
    "mirror_enabled": False,  # 启动时自动开启镜像
    "api_poll": False,        # API 直读：轮询 solo/get 获取真实坐标（绕过街景元数据诱饵），默认关
    "ai_auto": False,         # AI 自动分析：新回合自动抓图分析并自动对答案（需 .env 配 AI Key）
    "cookie_declined": {"tuxun": False, "geoguessr": False},  # 自动录入 Cookie 的“否”记忆
    "name_protect": {         # NameProtect：DOM 级替换页面上显示的昵称/ID（默认关）
        "enabled": False,
        "rules": [],          # 自动学习，也可手动填 [{"match": "原名", "replace": "别名"}, ...]
    },
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
             settings: { anti_decoy: true, near_m: 150, display_delay: 0.4, api_poll: false, ai_auto: false } };
  var panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483647;background:rgba(12,15,22,.93);color:#cfe3ff;border:1px solid #2a3350;border-radius:8px;font:12px/1.6 "Microsoft YaHei",sans-serif;padding:10px 14px;min-width:270px;box-shadow:0 6px 24px rgba(0,0,0,.55);user-select:none';
  panel.innerHTML =
    '<div id="tx-head" style="cursor:move;color:#FFD700;font-weight:bold;user-select:none">📍 图寻助手 <span id="tx-fold" style="float:right;color:#55627e;cursor:pointer">[收起]</span></div>' +
    '<div id="tx-body">' +
    '<div>原点: <span id="tx-o" style="color:#FFD700;font-family:Consolas,monospace">-</span></div>' +
    '<div>目前: <span id="tx-c" style="color:#69db7c;font-family:Consolas,monospace">-</span> <span id="tx-cd" style="color:#8fa3c2"></span></div>' +
    '<div>答案: <span id="tx-a" style="color:#4FC3F7;font-family:Consolas,monospace">-</span></div>' +
    '<div style="color:#8fa3c2">诱饵 <b id="tx-d" style="color:#d97a7a">0</b> · 候选 <b id="tx-cd2" style="color:#f0a35e">0</b> · 模式 <span id="tx-mv">-</span></div>' +
    '<div style="margin-top:5px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center">' +
    '<label style="cursor:pointer"><input type="checkbox" id="tx-ad"> 防诱饵</label>' +
    '<label style="cursor:pointer"><input type="checkbox" id="tx-ap"> API直读</label>' +
    '<label style="cursor:pointer"><input type="checkbox" id="tx-aia"> AI自动</label>' +
    '</div>' +
    '<div style="margin-top:5px;display:flex;gap:6px">' +
    '<button id="tx-copy" style="background:#1a1a2e;color:#00E5FF;border:1px solid #2a3350;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:11px">复制原点</button>' +
    '<button id="tx-hide" style="background:#1a1a2e;color:#9fb3d9;border:1px solid #2a3350;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:11px">隐藏面板</button>' +
    '</div><div id="tx-tip" style="color:#55627e;font-size:11px;margin-top:2px"></div></div>';
  function mount(){ document.body.appendChild(panel); }
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);

  var hidden = false;
  function fmt(p){ return p ? (+p.lat).toFixed(5) + ', ' + (+p.lng).toFixed(5) : '-'; }
  function render(){
    var o = document.getElementById('tx-o'); if (!o) return;
    o.innerText = fmt(st.origin);
    document.getElementById('tx-c').innerText = fmt(st.current);
    document.getElementById('tx-cd').innerText = (st.current && st.current.from_origin_m != null)
      ? '(' + Math.round(st.current.from_origin_m) + ' m)' : '';
    document.getElementById('tx-a').innerText = fmt(st.answer);
    document.getElementById('tx-d').innerText = st.decoys;
    document.getElementById('tx-cd2').innerText = st.candidates;
    document.getElementById('tx-mv').innerText = st.round_move === true ? '可移动' : (st.round_move === false ? '无移动' : '-');
    var boxes = { 'tx-ad': st.settings.anti_decoy, 'tx-ap': st.settings.api_poll, 'tx-aia': st.settings.ai_auto };
    for (var id in boxes) { var el = document.getElementById(id); if (el && document.activeElement !== el) el.checked = boxes[id]; }
  }
  function poll(){
    fetch(API + '/state').then(function(r){ return r.json(); }).then(function(j){ st = j; render(); }).catch(function(){});
  }
  function save(extra){
    var body = {};
    for (var k in st.settings) body[k] = st.settings[k];
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
    var map = { 'tx-ad': 'anti_decoy', 'tx-ap': 'api_poll', 'tx-aia': 'ai_auto' };
    if (map[e.target.id]) { var ex = {}; ex[map[e.target.id]] = e.target.checked; save(ex); }
  });
  document.addEventListener('click', function(e){
    if (e.target.id === 'tx-fold') {
      hidden = !hidden;
      document.getElementById('tx-body').style.display = hidden ? 'none' : 'block';
      document.getElementById('tx-fold').innerText = hidden ? '[展开]' : '[收起]';
    } else if (e.target.id === 'tx-copy') {
      var t = st.origin ? (+st.origin.lat).toFixed(6) + ', ' + (+st.origin.lng).toFixed(6) : '';
      if (t && navigator.clipboard) navigator.clipboard.writeText(t)
        .then(function(){ var tip=document.getElementById('tx-tip'); if(tip) {tip.innerText='已复制'; setTimeout(function(){tip.innerText='';},1500);} });
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
    var head = document.getElementById('tx-head');
    document.addEventListener('mousemove', function(e){ if(!on) return;
      panel.style.left = (ox + e.clientX - sx) + 'px'; panel.style.bottom = 'auto';
      panel.style.top = (oy + e.clientY - sy) + 'px'; });
    document.addEventListener('mouseup', function(){ on = false; });
    document.addEventListener('DOMContentLoaded', function(){ if(document.getElementById('tx-head'))
      document.getElementById('tx-head').addEventListener('mousedown', function(e){ on=true; sx=e.clientX; sy=e.clientY; e.preventDefault(); }); });
  })();
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

class MirrorRewrite:
    """反向代理响应改写：
    * 绝对地址改写到本地镜像（JS 里转义斜杠的写法一并处理）；
    * Set-Cookie 去掉 Domain/Secure（镜像域为 127.0.0.1 + http）；
    * 页面注入悬浮窗（原点/目前/答案 + 设置），数据来自控制 API；
    * 同时把流量委托给 TuxunInterceptor 做坐标捕获（反向模式下 host 仍是图寻）。
    """

    _UP = "tuxun.fun"
    _CDN = "https://b68res.daai.fun"

    def __init__(self, app: "TuxunApp", port: int, interceptor: "TuxunInterceptor",
                 control_port: int = 18080, jar: Optional[MirrorCookieJar] = None):
        self.app = app
        self.port = port
        self.interceptor = interceptor
        self.jar = jar or MirrorCookieJar(app)
        self.control_port = control_port
        self._local = f"http://127.0.0.1:{port}"
        self._ws_local = f"ws://127.0.0.1:{port}"

    def request(self, flow: "http.HTTPFlow") -> None:
        # 竞猜/上报请求格式记录（用于兼容性分析；值不含敏感信息）
        try:
            u = flow.request.pretty_url
            if "tuxun" in u and ("/game/report" in u or "/game/check" in u):
                logger.info("上报请求: %s", u[:400])
        except Exception:
            pass
        # CDN 资产改道（/cdn/ 前缀 -> b68res.daai.fun）+ 上游会话注入
        try:
            if flow.request.path.startswith("/cdn/"):
                flow.request.path = flow.request.path[len("/cdn"):]
                flow.request.host = "b68res.daai.fun"
                flow.request.scheme = "https"
                flow.request.port = 443
            elif flow.request.pretty_host.endswith(self._UP) or "127.0.0.1" in flow.request.pretty_host:
                ck = self.jar.load()
                if ck:
                    flow.request.headers["cookie"] = ck
        except Exception as exc:
            logger.debug("镜像请求改写失败: %s", exc)

    def _rewrite_text(self, text: str) -> str:
        # 图寻前端把 API/静态资源地址硬编码在 JS 里（含 CDN），全部改写到本地镜像
        text = text.replace(f"{self._CDN}", f"{self._local}/cdn")
        text = text.replace(f"https://www.{self._UP}", self._local)
        text = text.replace(f"https://{self._UP}", self._local)
        text = text.replace(f"https:\\/\\/www.{self._UP}", self._local)
        text = text.replace(f"https:\\/\\/{self._UP}", self._local)
        text = text.replace(f"wss://{self._UP}", self._ws_local)
        text = text.replace(f"wss:\\/\\/{self._UP}", self._ws_local)
        return text

    @staticmethod
    def _fix_set_cookie(headers) -> None:
        values = headers.get_all("set-cookie") if hasattr(headers, "get_all") else []
        if not values:
            return
        try:
            del headers["set-cookie"]
        except KeyError:
            pass
        import re as _re

        for v in values:
            v = _re.sub(r"Domain=[^;]+;?\s*", "", v, flags=_re.I)
            v = _re.sub(r"Secure;?\s*", "", v, flags=_re.I)
            headers.append("set-cookie", v)
        joined = "; ".join(values)
        if "fun_ticket=" in joined or "SESSION=" in joined:
            MirrorRewrite._cookie_store_update(joined)

    _cookie_store: dict = {}

    @staticmethod
    def _cookie_store_update(joined: str) -> None:
        MirrorRewrite._cookie_store["value"] = joined

    def response(self, flow: "http.HTTPFlow") -> None:
        # 坐标/对局捕获与主拦截完全一致（反向模式下 host 仍为图寻域名）
        self.interceptor.response(flow)
        if flow.response is None:
            return
        try:
            self._fix_set_cookie(flow.response.headers)
        except Exception as exc:
            logger.debug("Set-Cookie 改写失败: %s", exc)
        ctype = (flow.response.headers.get("content-type") or "").lower()
        if not any(t in ctype for t in ("text/html", "javascript", "json", "text/plain")):
            return
        try:
            text = flow.response.get_text(strict=False)
        except Exception:
            return
        if not text or len(text) > 8_000_000:
            return
        text = _META_CSP_RE.sub("", text)  # 页面内嵌 meta CSP 会拦注入脚本
        new = self._rewrite_text(text)
        injected = False
        if "text/html" in ctype and "</body>" in new.lower():
            pos = new.lower().rfind("</body>")
            injection = ("<script>" + _OVERLAY_SCRIPT.replace(
                "__CONTROL_PORT__", str(self.app.config.get("control_port", 18080))
            ) + "</script>")
            new = new[:pos] + injection + new[pos:]
            injected = True
        if new != text or injected:
            try:
                # 放宽 CSP（页面内嵌 meta 与响应头），禁止缓存旧壳页
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

    def websocket_message(self, flow: "http.HTTPFlow") -> None:
        self.interceptor.websocket_message(flow)


class MirrorCookieJar:
    """镜像会话的 Cookie 托管：优先使用 .env 的图寻 Cookie，并随服务器刷新。"""

    def __init__(self, app: "TuxunApp"):
        self.app = app
        self._file = os.path.join(BASE_DIR, "_mirror_session.txt")

    def load(self) -> str:
        stored = os.getenv("TUXUN_COOKIE", "").strip()
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


class ControlApiHandler:
    """悬浮窗的数据/设置 API（仅监听 127.0.0.1）。"""

    @staticmethod
    def make_handler(app: "TuxunApp"):
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

            def do_GET(self):
                if self.path != "/state":
                    self._send(404, {"error": "not found"})
                    return
                origin = app._round_anchor
                with app._lock:
                    cur = app._cur_pos
                    ans = app._last_answer
                    decoys = sum(1 for r in app._history if r.get("decoy"))
                    cands = sum(1 for r in app._history if r.get("candidate"))
                self._send(200, {
                    "origin": {"lat": origin[0][0], "lng": origin[0][1]} if origin else None,
                    "origin_trusted": bool(origin and origin[3]),
                    "current": cur,
                    "answer": ans,
                    "round_move": app._round_move,
                    "decoys": decoys,
                    "candidates": cands,
                    "settings": {
                        "anti_decoy": app.config.get("anti_decoy", True),
                        "near_m": app.config.get("near_m", 150),
                        "display_delay": app.config.get("display_delay", 0.4),
                        "api_poll": bool(app.config.get("api_poll")),
                        "ai_auto": bool(app.config.get("ai_auto")),
                    },
                })

            def do_POST(self):
                if self.path != "/settings":
                    self._send(404, {"error": "not found"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception as exc:
                    self._send(400, {"error": str(exc)})
                    return
                allowed = {"anti_decoy", "near_m", "display_delay", "api_poll", "ai_auto"}
                for k, v in body.items():
                    if k in allowed:
                        app.config[k] = v
                save_config(app.config)
                app.api_reader.enabled = bool(app.config.get("api_poll"))
                if app.config.get("ai_auto"):
                    app.init_ai_backend()
                logger.info("镜像悬浮窗更新设置: %s", applog.sanitize_json(body))
                self._send(200, {"status": "ok", "settings": {
                    "anti_decoy": app.config.get("anti_decoy", True),
                    "near_m": app.config.get("near_m", 150),
                    "display_delay": app.config.get("display_delay", 0.4),
                    "api_poll": bool(app.config.get("api_poll")),
                    "ai_auto": bool(app.config.get("ai_auto")),
                }})

            def log_message(self, *a):  # 静默访问日志
                pass

        return Handler

    @staticmethod
    def start(app: "TuxunApp", port: int) -> None:
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", port), ControlApiHandler.make_handler(app))
        threading.Thread(target=server.serve_forever, daemon=True, name="control-api").start()
        logger.info("悬浮窗控制 API: http://127.0.0.1:%d/state", port)


# ---------------------------------------------------------------------------
# 可重启的本地代理服务线程
# ---------------------------------------------------------------------------

class ProxyServer:
    """在独立线程里运行 mitmproxy；支持运行时切换上级代理（无需重启/释放端口）。"""

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

    def _mode_spec(self, upstream: str) -> str:
        listen = f"@127.0.0.1:{self.port}"
        return (f"upstream:{upstream}{listen}") if upstream else f"regular{listen}"

    def start(self, upstream: str = "") -> bool:
        """启动服务；已在运行时仅热切换模式，返回是否就绪。"""
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
        """运行时切换 regular/upstream 模式（mitmproxy 支持在线重配置）。"""
        if self._master is None or self._loop is None:
            return
        spec = self._mode_spec(upstream)

        def _apply():
            try:
                self._master.options.mode = [spec]
                self.upstream = upstream
                logger.info("代理模式已切换: %s", spec)
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
            opts = Options(mode=[self._mode_spec(upstream)])
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
            master.addons.add(TuxunInterceptor(self.app), NameProtect(self.app))
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

class TuxunApp:
    """负责捕获点分发、逆地理编码、系统代理托管与 GUI/控制台展示。"""

    def __init__(self, config: dict, console_only: bool = False):
        self.config = config
        self.console_only = console_only or not WEBVIEW_AVAILABLE
        self.port = int(config.get("proxy_port", 8080))
        self.window = None
        self.server = ProxyServer(self, self.port)
        self.api_reader = SoloApiReader(self)
        self._history: List[dict] = []
        self._lock = threading.Lock()
        self._last = None           # (lat, lng) 去重
        self._round_anchor = None   # (key, pano, ts, trusted) 本回合锚点（反作弊）
        self._candidates: dict = {}  # pano -> (lat, lng) 存疑候选（等待 API 校准）
        self._round_move = None     # 当前回合移动模式（True 可移动 / False 无移动 / None 未知）
        self._cur_pos = None        # {'lat','lng','from_origin_m','time'} 玩家目前位置
        self._last_answer = None    # {'lat','lng','time'} 最近一次揭示的答案
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

    # ------------------------------------------------------------------
    # Cookie 自动录入
    # ------------------------------------------------------------------
    def note_platform_cookie(self, plat: str, cookie_header: str) -> None:
        """拦截到平台请求的 Cookie 头：未录入且未拒绝时，发起“是否录入”询问。"""
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
        address = "" if (decoy or candidate or moved) else reverse_geocode(
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
                             "from_origin_m": from_origin_m, "time": record["time"]}
        elif not decoy and not candidate:
            self._cur_pos = {"lat": record["lat"], "lng": record["lng"], "time": record["time"]}
            if source == "积分赛答案揭示":
                self._last_answer = {"lat": record["lat"], "lng": record["lng"],
                                     "time": record["time"]}
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

    # ------------------------------------------------------------------
    # 供网页 GUI 调用的 JS API
    # ------------------------------------------------------------------
    def get_initial_api(self) -> dict:
        return dict(self.config)

    def save_config_api(self, data) -> dict:
        try:
            data = json.loads(data) if isinstance(data, str) else data
            self.config.update(data)
            save_config(self.config)
            return {"status": "success", "message": "配置已保存"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def open_proxy_api(self) -> dict:
        ok, message = self.enable_interception()
        if ok:
            self.config["proxy_enabled"] = True
            save_config(self.config)
            return {"status": "success", "message": message}
        return {"status": "error", "message": message}

    def close_proxy_api(self) -> dict:
        ok, message = self.disable_interception()
        if ok:
            self.config["proxy_enabled"] = False
            save_config(self.config)
            return {"status": "success", "message": message}
        return {"status": "error", "message": message}

    def copy_text_api(self, text) -> dict:
        text = str(text)
        try:
            if WINDOWS:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Set-Clipboard", "-Value", text],
                    check=False, timeout=10,
                )
            elif sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
            else:
                subprocess.run(["xclip", "-selection", "clipboard"],
                               input=text.encode("utf-8"), check=False)
            return {"status": "success", "message": "已复制到剪贴板"}
        except Exception as exc:
            return {"status": "error", "message": f"复制失败: {exc}"}

    def open_url_api(self, url: str) -> dict:
        try:
            webbrowser.open(url)
            return {"status": "success", "message": ""}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def toggle_ai_auto(self, enabled) -> dict:
        self.config["ai_auto"] = bool(enabled)
        save_config(self.config)
        if enabled:
            if self.init_ai_backend():
                return {"status": "success",
                        "message": f"AI 自动分析已开启（{self.ai_backend.model_name}），"
                                   "检测到新回合会自动抓图分析并对答案"}
            return {"status": "info",
                    "message": "已开启，但未配置 AI Key：请在 .env 填写 "
                               "OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL 后重启程序"}
        return {"status": "success", "message": "AI 自动分析已关闭"}

    def set_api_poll(self, enabled) -> dict:
        self.config["api_poll"] = bool(enabled)
        save_config(self.config)
        self.api_reader.enabled = bool(enabled)
        if enabled:
            return {"status": "success",
                    "message": "API 直读已开启：读取浏览器自身的对局响应获取真实坐标（被动零请求）"}
        return {"status": "success", "message": "API 直读已关闭"}

    # ------------------------------------------------------------------
    # GUI
    # ------------------------------------------------------------------
    def build_html(self) -> str:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.html"),
                  "r", encoding="utf-8") as f:
            html = f.read()
        return html.replace("__CONFIG_JSON__", json.dumps(self.config, ensure_ascii=False))

    def run_gui(self) -> None:
        self.window = webview.create_window(
            "图寻助手 · 实时取点",
            html=self.build_html(),
            width=980, height=700,
            on_top=True,
            js_api=self,
        )
        webview.start()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def print_banner(app: TuxunApp, auto_proxy: bool, mirror_on: bool = False) -> None:
    upstream, upstream_desc = app._resolve_upstream()
    print("=" * 56)
    print("  图寻助手 · 实时取点（支持 图寻 / GeoGuessr）")
    print(f"  本地代理: http://127.0.0.1:{app.port}")
    if WINDOWS:
        print(f"  系统代理: {'已接管' if auto_proxy else '未接管（点击界面/查看下方提示）'}")
    else:
        print(f"  非 Windows：请在浏览器中手动设置 HTTP 代理为 127.0.0.1:{app.port}")
    print(f"  上级代理: {upstream_desc + '（自动级联）' if upstream_desc else '直连'}")
    if mirror_on:
        mport = int(app.config.get("mirror_port", 8001))
        print(f"  图寻镜像: http://127.0.0.1:{mport}（免证书，浏览器直接访问做题）")
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


def start_mirror_server(app: "TuxunApp", port: int, control_port: int) -> None:
    """图寻镜像：反向代理 + 页面注入 + 悬浮窗控制 API（免证书、免系统代理）。"""
    ControlApiHandler.start(app, control_port)
    jar = MirrorCookieJar(app)

    def _run() -> None:
        async def _m() -> None:
            opts = Options(mode=[f"reverse:https://tuxun.fun@127.0.0.1:{port}"])
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
            interceptor = TuxunInterceptor(app)
            master.addons.add(MirrorRewrite(app, port, interceptor, control_port, jar))
            await master.run()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_m())
        except Exception as exc:  # noqa: BLE001
            logger.error("镜像服务异常退出: %s", exc)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True, name="mirror").start()
    deadline = time.time() + 10
    while time.time() < deadline:
        if port_listening("127.0.0.1", port, timeout=0.4):
            return
        time.sleep(0.15)
    logger.warning("镜像端口 %d 未就绪。", port)


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(errors="replace", line_buffering=True)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="图寻助手 · 实时取点模式（本地代理拦截街景坐标）")
    parser.add_argument("--console", action="store_true", help="纯控制台模式，不启动图形界面")
    parser.add_argument("--port", type=int, help="本地代理端口（默认读取 config.json，初始 8080）")
    parser.add_argument("--proxy", action="store_true", help="启动后立即开启拦截并接管系统代理")
    parser.add_argument("--mirror", action="store_true",
                        help="开启图寻镜像（免证书免系统代理）：浏览器访问 http://127.0.0.1:镜像端口 做题")
    parser.add_argument("--install-cert", action="store_true", help="安装 mitmproxy 根证书后退出")
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
    if args.port:
        config["proxy_port"] = args.port
    load_dotenv(os.path.join(BASE_DIR, ".env"))  # NameProtect 学习需要读取 Cookie
    log_file = applog.setup(filename="proxy")
    logger.info("========== 实时取点模式启动 ==========")
    logger.info("Python %s | %s | 日志文件: %s", sys.version.split()[0], sys.platform, log_file)

    app = TuxunApp(config, console_only=args.console or not WEBVIEW_AVAILABLE)
    if args.console and not WEBVIEW_AVAILABLE:
        print("[提示] 未安装 pywebview，已自动降级为控制台模式。")
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
    mirror_on = bool(args.mirror or config.get("mirror_enabled"))
    if mirror_on:
        mport = int(config.get("mirror_port", 8001))
        cport = int(config.get("control_port", 18080))
        start_mirror_server(app, mport, cport)
        if port_listening("127.0.0.1", mport):
            logger.info("图寻镜像已就绪: http://127.0.0.1:%d（悬浮窗 API: %d）", mport, cport)

    auto_proxy = False
    if not args.no_system_proxy and (args.proxy or config.get("proxy_enabled")):
        ok, message = app.enable_interception()
        auto_proxy = ok
        print(f"[{ 'OK' if ok else '失败' }] {message}")
        logger.info("启动时自动开启拦截: %s -> %s", ok, message)
    elif not WINDOWS and not args.no_system_proxy:
        print(f"[提示] 请在浏览器中手动设置 HTTP 代理为 127.0.0.1:{app.port}")

    print_banner(app, auto_proxy, mirror_on)

    # 代理服务始终先启动（拦截开关只负责接管/还原系统代理）
    if not app.server.running:
        upstream, _ = app._resolve_upstream()
        app.server.start(upstream)
        print(f"本地代理已启动: http://127.0.0.1:{app.port}，等待捕获街景坐标 ...\n")

    if app.console_only:
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
    else:
        app.run_gui()
    logger.info("========== 程序退出 ==========")


if __name__ == "__main__":
    main()
