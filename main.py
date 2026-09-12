#!/usr/bin/env python3
"""main.py — 图寻助手 · AI 分析模式。

三种检测模式：
  1. 单图寻      —— 只走图寻 API
  2. 单GeoGuessr —— 只走 GeoGuessr API（扩展点已预留，待实现）
  3. 混合检测    —— 根据输入自动识别平台（URL 按域名，纯 ID 按 图寻→GeoGuessr 顺序尝试）

能力：
  * 未结束的对局：抓取街景图 → 视觉大模型定位（Gemini / 国内直连 GLM-4V 等）
  * 已结束的对局：自动进入「复盘模式」，直接给出每轮真实坐标与地址（无需 AI）
  * --skill 载入 TuxunSkill Meta 知识库规范，增强模型分析
  * --pano 直接分析任意 Google 街景 Pano ID，无需账号
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import webbrowser
from datetime import datetime
from typing import List, Optional, Tuple

from dotenv import load_dotenv

import applog
from ai_client import AIBackend, AIError, build_backend_from_env
from game_sources import (
    GameInfo,
    GeoGuessrError,
    RoundInfo,
    detect_platforms,
    extract_game_id,
    filter_platforms,
    make_source,
    source_label,
)
from geocode import (
    amap_uri_link,
    google_maps_link,
    haversine_km,
    osm_link,
    reverse_geocode,
)
from pano_images import UnsupportedSourceError, views_for_source
from tuxun_agent import TuxunAgent, TuxunAPIError

BASE_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)  # PyInstaller onefile：配置/日志跟随 exe
    else os.path.dirname(os.path.abspath(__file__))
)
HISTORY_FILE = os.path.join(BASE_DIR, "history.jsonl")
DEFAULT_SKILL_DIR = os.path.join(BASE_DIR, "TuxunSkill")
log = logging.getLogger("tuxun.main")

PROMPT_TEMPLATE = """你是一位顶级的图寻（GeoGuessr）专家和地理学家。
下面给你 {n} 张从同一点拍摄的街景图片，拍摄方向依次为：{labels}。
请综合所有可见线索（道路标线与行车方向、护栏/电线杆/路缘石样式、路牌与招牌的语言文字、
车牌颜色样式、植被与地形、太阳方位、相机画质与拍摄车特征等）进行 meta 分析，
判断拍摄点的具体位置。

请严格按照以下格式输出，不要添加任何多余的文字、解释或编号：

关键线索: 一句话总结最关键的判断依据
大洲: [最可能的大洲]
国家: [最可能的国家] ([在所属大洲的大致方位])
省/州: [最可能的省份或州] ([在所属国家的大致方位])
城市: [最可能的城市] ([在所属省/州的大致方位])
置信度: [高/中/低]
坐标: [十进制纬度], [十进制经度]

注意：
1. 坐标使用 WGS84 十进制格式（示例：41.9028, 12.4964），精确到小数点后 4 位；
2. 不确定的层级请填写"未知"；
3. 坐标给出你判断位置的大致中心点。
"""

SKILL_BRIDGE = """

【任务】请遵循上方 TuxunSkill 知识库规范，分析下方提供的街景图片并给出定位结论
（输出格式沿用技能规范：定位结论/候选排名/关键观察/判断依据）。
与技能规范唯一的不同：必须在「定位结论」之后额外输出一行：
坐标: [十进制纬度], [十进制经度]
"""

GRID_NOTE_4 = "图片是一张拼合图：上排从左到右为 前、右 两个方向，下排从左到右为 后、左 两个方向，最下方的横条为天空（朝天拍摄）。"
GRID_NOTE_8 = "图片是一张拼合图：上排从左到右为 前、右前、右、右后 四个方向，下排从左到右为 后、左后、左、左前 四个方向，最下方的横条为天空（朝天拍摄）。"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def parse_coords(text: str) -> Optional[Tuple[float, float]]:
    """从模型输出中解析「坐标: 纬度, 经度」。"""
    match = re.search(r"坐标\s*[:：]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    if abs(lat) > 90 or abs(lng) > 180:
        return None
    return lat, lng


def load_skill(skill_dir: str) -> str:
    """载入 TuxunSkill 的 SKILL.md（知识库规范），过长时截断。"""
    path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(path):
        raise FileNotFoundError(f"未找到技能文件: {path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if len(text) > 24000:  # 控制提示词体积
        text = text[:24000] + "\n\n（……知识库规范过长已截断）"
    return text


def make_grid_image(image_bytes_list: List[bytes]) -> bytes:
    """把多张图拼成一张大图（用于只支持单图输入的模型，如 GLM-4V-Flash）。"""
    try:
        from PIL import Image
    except ImportError:
        raise AIError("拼图模式需要 Pillow 库：pip install pillow")

    sky_bytes = None
    if len(image_bytes_list) >= 3:  # 最后一张约定为天空图
        sky_bytes = image_bytes_list[-1]
        tiles = [Image.open(io.BytesIO(b)).convert("RGB") for b in image_bytes_list[:-1]]
    else:
        tiles = [Image.open(io.BytesIO(b)).convert("RGB") for b in image_bytes_list]

    count = len(tiles)
    cols = 2 if count <= 4 else 4
    rows = (count + cols - 1) // cols
    w, h = tiles[0].size
    grid_w, grid_h = cols * w, rows * h

    if sky_bytes is not None:
        sky_img = Image.open(io.BytesIO(sky_bytes)).convert("RGB")
        sky_img = sky_img.resize((grid_w, int(grid_w * sky_img.height / sky_img.width)))
        total_h = grid_h + sky_img.height
    else:
        total_h = grid_h

    canvas = Image.new("RGB", (grid_w, total_h), (0, 0, 0))
    for i, tile in enumerate(tiles):
        canvas.paste(tile.resize((w, h)), ((i % cols) * w, (i // cols) * h))
    if sky_bytes is not None:
        canvas.paste(sky_img, (0, grid_h))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def save_history(record: dict) -> None:
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info("历史已追加到 history.jsonl（类型: %s）", record.get("kind", "guess"))
    except OSError as exc:
        print(f"[警告] 历史记录写入失败: {exc}")


# ---------------------------------------------------------------------------
# AI 分析
# ---------------------------------------------------------------------------

def download_views(
    pano_id: str,
    args,
    pano_source: str = "google_pano",
    heading: Optional[float] = None,
) -> List[Tuple[str, bytes]]:
    """按图源获取方向视图。google 走缩略图通道，腾讯走瓦片拼接通道。"""
    if pano_source in ("qq_pano", "qq"):
        print("腾讯街景：正在下载全景瓦片并裁剪方向视图 ...")
        return views_for_source(
            "qq_pano", pano_id,
            heading=heading, count=args.directions,
            include_sky=not args.no_sky, size=640,
        )
    if pano_source in ("baidu_pano", "baidu"):
        return views_for_source("baidu_pano", pano_id)
    urls = TuxunAgent.get_directional_image_urls(
        pano_id,
        count=args.directions,
        include_sky=not args.no_sky,
        width=args.width,
        height=args.height,
    )
    if args.print_urls:
        for name, url in urls:
            print(f"  - {name}: {url}")
    print(f"正在下载 {len(urls)} 张街景图片 ...")
    views: List[Tuple[str, bytes]] = []
    for name, url in urls:
        image_bytes = TuxunAgent.download_image(url, timeout=args.timeout)
        views.append((name, image_bytes))
        print(f"  - {name} 视图已下载 ({len(image_bytes) // 1024} KB)")
    return views


def analyze_pano(
    backend: AIBackend,
    pano_id: str,
    args,
    game_id: str = "",
    round_no: int = 0,
    truth: Optional[Tuple[float, float]] = None,
    pano_source: str = "google_pano",
    heading: Optional[float] = None,
) -> None:
    """对一个街景 Pano 完成 取图 -> 分析 -> 反查地址 -> 记录 的全流程。"""
    views = download_views(pano_id, args, pano_source=pano_source, heading=heading)
    labels = [name for name, _ in views]

    skill_note = ""
    if args.skill:
        skill_note = SKILL_BRIDGE

    if args.grid:
        layout = GRID_NOTE_8 if len(labels) - 1 >= 8 else GRID_NOTE_4
        prompt = PROMPT_TEMPLATE.format(n=1, labels="见下方拼合图说明") + f"\n拼合图说明：{layout}\n" + skill_note
        print("正在拼合图片（单图模型模式）...")
        grid_bytes = make_grid_image([data for _, data in views])
        pairs = [("", grid_bytes)]
    else:
        prompt = PROMPT_TEMPLATE.format(n=len(labels), labels="、".join(labels)) + skill_note
        pairs = list(zip(labels, [data for _, data in views]))

    if args.skill:
        print("已载入 TuxunSkill 知识库规范 ...")

    print(f"正在请求模型分析（{backend.describe()}）...")
    analysis = backend.analyze(prompt, pairs)

    print("\n--- 模型分析结果 ---")
    print(analysis)
    print("--------------------")

    coords = parse_coords(analysis)
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "game_id": game_id,
        "round": round_no,
        "pano_id": pano_id,
        "model": backend.model_name,
    }
    if coords:
        lat, lng = coords
        address = reverse_geocode(lat, lng)
        print(f"\n解析坐标: {lat:.6f}, {lng:.6f}")
        if address:
            print(f"参考地址: {address}")
        print(f"OSM:        {osm_link(lat, lng)}")
        print(f"Google地图: {google_maps_link(lat, lng)}")
        if 3.0 <= lat <= 55.0 and 73.0 <= lng <= 136.0:
            print(f"高德地图:   {amap_uri_link(lat, lng)}")
        if truth:
            error_km = haversine_km(lat, lng, truth[0], truth[1])
            print(f"与真实答案的距离: {error_km:.1f} km")
        if args.open:
            webbrowser.open(google_maps_link(lat, lng))
        record["coords"] = [lat, lng]
        record["address"] = address
        if truth:
            record["truth"] = list(truth)
            record["error_km"] = round(haversine_km(lat, lng, truth[0], truth[1]), 2)
    else:
        print("\n[提示] 未能从输出中解析出坐标行。")

    if not args.no_history:
        record["analysis"] = analysis
        save_history(record)
        print(f"已保存到 {os.path.basename(HISTORY_FILE)}")


# ---------------------------------------------------------------------------
# 复盘模式（已结束的对局，无需 AI）
# ---------------------------------------------------------------------------

def review_game(game: GameInfo, backend: Optional[AIBackend], args) -> None:
    print(f"\n=== 复盘模式：{game.platform} 对局 {game.game_id} ===")
    print(f"共 {len(game.rounds)} 回合，状态: {game.status}"
          + (f"，总分: {game.score}" if game.score is not None else ""))
    round_records = []
    for r in game.rounds:
        if r.lat is None or r.lng is None:
            print(f"第 {r.round_no} 轮: 真实坐标不可用（对局未结束）")
            round_records.append({"round": r.round_no, "source": r.source, "coords": None})
            continue
        address = reverse_geocode(r.lat, r.lng, coord=r.coord_sys)
        label = source_label(r.source)
        print(f"\n第 {r.round_no} 轮 [{label}] 真实坐标: {r.lat:.6f}, {r.lng:.6f}")
        if address:
            print(f"  地址: {address}")
        print(f"  OSM: {osm_link(r.lat, r.lng)}")
        print(f"  Google: {google_maps_link(r.lat, r.lng)}")
        if 3.0 <= r.lat <= 55.0 and 73.0 <= r.lng <= 136.0:
            print(f"  高德: {amap_uri_link(r.lat, r.lng, coord=r.coord_sys)}")
        round_records.append({
            "round": r.round_no,
            "source": r.source,
            "pano_id": r.pano_id,
            "coords": [r.lat, r.lng],
            "address": address,
        })

        # 谷歌图源且开启 --ai-review：AI 猜一把并与真值对比
        if args.ai_review and backend is not None and r.source == "google_pano" and r.pano_id:
            print("  --- AI 复盘 ---")
            try:
                analyze_pano(
                    backend, r.pano_id, args,
                    game_id=game.game_id, round_no=r.round_no,
                    truth=(r.lat, r.lng),
                )
            except (AIError, TuxunAPIError) as exc:
                print(f"  AI 复盘失败: {exc}")
        elif args.ai_review and r.source != "google_pano":
            print("  （国内图源暂不支持 AI 取图复盘，可用实时取点模式）")

    if not args.no_history:
        save_history({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "review",
            "platform": game.platform,
            "game_id": game.game_id,
            "status": game.status,
            "score": game.score,
            "rounds": round_records,
        })


# ---------------------------------------------------------------------------
# 模式调度
# ---------------------------------------------------------------------------

def choose_mode_interactive() -> str:
    print("\n请选择检测模式：")
    print("  [1] 单图寻 (tuxun)")
    print("  [2] 单 GeoGuessr (实验性)")
    print("  [3] 混合检测（自动识别输入属于哪个平台）")
    while True:
        choice = input("输入 1/2/3 (回车默认 3): ").strip()
        if choice in ("", "3"):
            return "auto"
        if choice == "1":
            return "tuxun"
        if choice == "2":
            return "geoguessr"
        print("无效输入，请输入 1、2 或 3。")


def run_detection(backend: Optional[AIBackend], cookies: dict, args, user_input: str) -> None:
    """混合调度：识别平台 -> 拉取对局 -> 未结束走 AI / 已结束走复盘。"""
    platforms = filter_platforms(args.mode, detect_platforms(user_input))
    game_id = extract_game_id(user_input)
    if not game_id:
        print("错误：未能从输入中识别出游戏 ID。")
        return

    handled = False
    for platform in platforms:
        try:
            source = make_source(platform, cookies)
        except ValueError as exc:
            print(f"\n[{platform}] {exc}")
            continue
        try:
            if platform != "tuxun":
                source.verify()  # 校验凭证（未配置会给出明确提示）
            game = source.get_game(game_id)
        except NotImplementedError as exc:
            print(f"\n[{platform}] {exc}")
            continue
        except (TuxunAPIError, GeoGuessrError) as exc:
            print(f"\n[{platform}] {exc}")
            continue

        handled = True
        if game.finished_with_truth:
            review_game(game, backend, args)
        else:
            rnd = game.current_round
            if rnd is None or not rnd.pano_id:
                print(f"[{platform}] 对局存在但没有可分析的回合。")
                continue
            if rnd.source in ("baidu_pano", "baidu"):
                print(
                    f"\n[{platform}] 当前回合图源为「{source_label(rnd.source)}」，"
                    "暂无公开免凭证取图通道。"
                )
                print("建议：使用实时取点模式（tuxun_proxy.py），或等对局结束后用复盘模式。")
                continue
            print(f"[{platform}] 第 {rnd.round_no} 回合 [{source_label(rnd.source)}]，Pano ID: {rnd.pano_id}")
            truth = (rnd.lat, rnd.lng) if rnd.lat is not None and rnd.lng is not None else None
            analyze_pano(backend, rnd.pano_id, args,
                         game_id=game.game_id, round_no=rnd.round_no,
                         truth=truth, pano_source=rnd.source, heading=rnd.heading)
        break  # 单平台成功处理后即结束

    if not handled:
        print("\n所有平台均未能处理该输入。")


def describe_draw(info: dict) -> str:
    """把抽奖结果翻译成人类可读的奖励描述。"""
    parts = []
    vip_ms = info.get("rewardVipMs") or 0
    gems = info.get("rewardGems") or 0
    if info.get("type") == "vip" and info.get("days"):
        parts.append(f"图寻会员 {info['days']} 天")
    if vip_ms:
        parts.append(f"图寻会员 {vip_ms / 86400000:.1f} 天")
    if gems:
        parts.append(f"{gems} 钻石")
    if info.get("hours"):
        parts.append(f"{info['hours']} 小时")
    if info.get("minutes"):
        parts.append(f"{info['minutes']} 分钟")
    if not parts:
        parts.append("谢谢参与（未中奖）")
    day = info.get("dayStr")
    return f"[{day}] " + " + ".join(parts) if day else " + ".join(parts)


def run_draw(cookies: dict) -> None:
    """--draw：跑一遍所有每日抽奖（每挑战抽奖 + 每日任务抽奖）。"""
    if not cookies.get("tuxun"):
        print("错误：未配置 TUXUN_COOKIE。")
        return
    try:
        agent = TuxunAgent(cookies["tuxun"])
        base = agent.base_url

        # 1) 完成每日挑战的抽奖
        resp = agent.session.get(f"{base}/api/v0/tuxun/draw/checkDailyChallenge", timeout=10)
        data = (resp.json().get("data") or {})
        if data.get("status") == "drawed":
            print(f"[每挑战抽奖] 今天已抽过 → {describe_draw(data.get('drawResult') or {})}")
        else:
            resp = agent.session.get(f"{base}/api/v0/tuxun/draw/dailyChallenge", timeout=10)
            data = (resp.json().get("data") or {}) or {}
            day = data.get("dayStr", "?")
            print(f"[每挑战抽奖] 抽奖完成 → [{day}] {describe_draw(data)}")

        # 2) 每日任务抽奖（完成每日挑战后解锁，可抽会员）
        time.sleep(1.5)
        resp = agent.session.get(f"{base}/api/v0/tuxun/task/dailyLottery/status", timeout=10)
        lot = (resp.json().get("data") or {})
        if lot.get("canDraw"):
            resp = agent.session.post(f"{base}/api/v0/tuxun/task/dailyLottery/draw", timeout=10)
            reward = (resp.json().get("data") or {}) or {}
            print(f"[每日任务抽奖] 抽奖完成 → {describe_draw(reward)}")
        elif lot.get("drawnToday"):
            print(f"[每日任务抽奖] 今天已抽过 → {describe_draw(lot.get('lastReward') or {})}")
        else:
            print("[每日任务抽奖] 暂不可抽：完成今日每日挑战后解锁。")
    except Exception as exc:  # noqa: BLE001
        print(f"抽奖失败: {exc}")


def _fmt_ts(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def run_history(backend: Optional[AIBackend], cookies: dict, args) -> None:
    """--history：列出最近对局并复盘（不带 N 交互选择，带 N 批量复盘最近 N 局）。"""
    try:
        source = make_source("tuxun", cookies)
    except ValueError as exc:
        print(f"错误：{exc}")
        return
    try:
        games = source.agent.get_history(page=1, page_size=20)
    except TuxunAPIError as exc:
        print(f"获取历史对局失败: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"获取历史对局失败: {exc}")
        return
    if not games:
        print("历史记录为空。")
        return

    if args.history == -1:  # 交互选择
        print(f"\n=== 最近 {len(games)} 局 ===")
        for i, g in enumerate(games, 1):
            print(f"  [{i}] {_fmt_ts(g.get('gmtCreate'))}  {g.get('type')}  "
                  f"score={g.get('score')}  {g.get('gameId')}")
        raw = input("选择要复盘的局号 (回车=1, q=返回): ").strip()
        if raw.lower() == "q":
            return
        try:
            idx = int(raw) if raw else 1
        except ValueError:
            print("无效输入。")
            return
        if not 1 <= idx <= len(games):
            print("超出范围。")
            return
        picks = [games[idx - 1]]
    else:
        picks = games[:max(1, args.history)]
        print(f"将依次复盘最近 {len(picks)} 局（每局间隔 1.2 秒，温和请求）...")

    for i, g in enumerate(picks):
        if i:
            time.sleep(1.2)
        try:
            game = source.get_game(g["gameId"])
        except TuxunAPIError as exc:
            print(f"[{g.get('gameId')}] 跳过: {exc}")
            continue
        if game.finished_with_truth:
            review_game(game, backend, args)
        else:
            print(f"[{g.get('gameId')}] 对局未结束或无真实坐标，跳过复盘。")


def run_geo_history(backend: Optional[AIBackend], cookies: dict, args) -> None:
    """--history 的 GeoGuessr 版本：从个人动态解析最近对局并复盘。"""
    try:
        source = make_source("geoguessr", cookies)
        games = source.list_recent(20)
    except ValueError as exc:
        print(f"错误：{exc}")
        return
    except GeoGuessrError as exc:
        print(f"获取动态失败: {exc}")
        return
    if not games:
        print("个人动态里没有找到对局记录（feed 只保留近期条目）。")
        return

    if args.history == -1:  # 交互选择
        print(f"\n=== GeoGuessr 最近 {len(games)} 局（来自个人动态）===")
        for i, g in enumerate(games, 1):
            print(f"  [{i}] {g['time'][:16] if g['time'] else '?'}  {g['mode']}  {g['game_id']}")
        raw = input("选择要复盘的局号 (回车=1, q=返回): ").strip()
        if raw.lower() == "q":
            return
        try:
            idx = int(raw) if raw else 1
        except ValueError:
            print("无效输入。")
            return
        if not 1 <= idx <= len(games):
            print("超出范围。")
            return
        picks = [games[idx - 1]]
    else:
        picks = games[:max(1, args.history)]
        print(f"将依次复盘最近 {len(picks)} 局 ...")

    for i, g in enumerate(picks):
        if i:
            time.sleep(1.2)
        try:
            game = source.get_game(g["game_id"])
        except GeoGuessrError as exc:
            mode = (g.get("mode") or "?").lower()
            hint = "（Duels 系列对战走独立端点，暂不支持读取；Classic/挑战类对局不受影响）" if "duel" in mode else ""
            print(f"[{g['game_id']}] 跳过: {exc}{hint}")
            continue
        if game.finished_with_truth:
            review_game(game, backend, args)
        else:
            print(f"[{g['game_id']}] 对局无真实坐标，跳过复盘。")


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="图寻助手 · AI 分析模式（支持图寻/GeoGuessr 三模式，Gemini 与国内直连模型）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("game_id", nargs="?", help="游戏 ID 或完整对局 URL（图寻/GeoGuessr 均可）")
    parser.add_argument("--mode", choices=["tuxun", "geoguessr", "auto"], default=None,
                        help="检测模式；不指定且为交互模式时会弹出菜单")
    parser.add_argument("--pano", help="直接分析指定的 Google 街景 Pano ID（无需任何账号）")
    parser.add_argument("--model", help="覆盖 .env 中配置的模型名")
    parser.add_argument("--directions", type=int, choices=[4, 8], default=4,
                        help="水平方向数量：4（前后左右）或 8（含斜向）")
    parser.add_argument("--no-sky", action="store_true", help="不拍摄天空视角")
    parser.add_argument("--grid", action="store_true",
                        help="多图拼成一张大图再发给模型（适配仅支持单图的模型，如 glm-4v-flash）")
    parser.add_argument("--skill", nargs="?", const=DEFAULT_SKILL_DIR, default=None,
                        metavar="SKILL_DIR",
                        help="载入 TuxunSkill 知识库规范增强分析（默认自动寻找 ./TuxunSkill）")
    parser.add_argument("--history", nargs="?", const=-1, default=None, type=int, metavar="N",
                        help="复盘最近对局：不带 N 交互选择，带 N 直接复盘最近 N 局")
    parser.add_argument("--draw", action="store_true",
                        help="每日挑战页的「抽图寻会员」抽奖（已抽过则只报告结果）")
    parser.add_argument("--ai-review", action="store_true",
                        help="复盘已结束对局时，让 AI 也猜一遍并计算与真实答案的误差")
    parser.add_argument("--width", type=int, default=1024, help="街景图宽度")
    parser.add_argument("--height", type=int, default=768, help="街景图高度")
    parser.add_argument("--timeout", type=int, default=15, help="单张图片下载超时（秒）")
    parser.add_argument("--open", action="store_true", help="分析完成后自动在浏览器打开 Google Maps")
    parser.add_argument("--print-urls", action="store_true", help="打印街景图片 URL")
    parser.add_argument("--no-history", action="store_true", help="不写入 history.jsonl")
    args = parser.parse_args()

    load_dotenv(os.path.join(BASE_DIR, ".env"))
    log_file = applog.setup(filename="main")
    _log = logging.getLogger("tuxun.main")
    _log.info("========== AI 分析模式启动 ==========")
    _log.info("参数: mode=%s pano=%s directions=%s skill=%s history=%s draw=%s",
              args.mode, bool(args.pano), args.directions, bool(args.skill),
              args.history, args.draw)

    # 国内网络：设置 PROXY_URL 后，Google 街景图与 Gemini 的请求将走该代理。
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if proxy_url:
        os.environ.setdefault("HTTPS_PROXY", proxy_url)
        os.environ.setdefault("HTTP_PROXY", proxy_url)
        print(f"已启用代理: {proxy_url}（仅用于 Google 街景图 / Gemini）")

    # AI 后端：复盘模式可以完全不用 AI
    backend: Optional[AIBackend] = None
    ai_required = args.pano or args.ai_review
    try:
        backend = build_backend_from_env(os.environ)
        print(f"AI 后端: {backend.describe()}")
    except AIError as exc:
        if ai_required:
            print(f"错误：{exc}")
            sys.exit(1)
        print("未配置 AI 后端：仅支持复盘已结束的对局（无需 AI）。")
    if backend is not None and args.model:
        if backend.provider == "openai":
            backend.openai_model = args.model
        else:
            backend.gemini_model = args.model

    if args.skill:
        try:
            load_skill(args.skill)
            skill_name = os.path.basename(os.path.normpath(args.skill))
            print(f"TuxunSkill 已就绪: {skill_name}")
        except FileNotFoundError as exc:
            print(f"[警告] {exc}，已忽略 --skill")
            args.skill = None

    # 凭证
    cookies = {
        "tuxun": os.getenv("TUXUN_COOKIE", "").strip(),
        "geoguessr": os.getenv("GEOGUESSR_COOKIE", "").strip(),
    }
    if not cookies["tuxun"]:
        print("提示：未配置 TUXUN_COOKIE（.env），将无法访问图寻 API。")

    # --draw：每日会员抽奖
    if args.draw:
        run_draw(cookies)
        return

    # --history：历史对局复盘（按模式路由到对应平台）
    if args.history is not None:
        if args.mode == "geoguessr":
            run_geo_history(backend, cookies, args)
        else:
            run_history(backend, cookies, args)
        return

    # --pano：不经过任何平台，直接分析 Google 街景
    if args.pano:
        try:
            analyze_pano(backend, args.pano, args)
        except (TuxunAPIError, AIError) as exc:
            print(f"出错: {exc}")
        return

    # 交互模式且未指定 mode 时弹出三模式菜单
    if args.mode is None and not args.game_id:
        args.mode = choose_mode_interactive()
    elif args.mode is None:
        args.mode = "auto"

    try:
        if args.game_id:
            run_detection(backend, cookies, args, args.game_id)
        else:
            print("\n--- 自动化图寻助手已启动 ---")
            print("可直接粘贴：图寻游戏ID / tuxun.fun 链接 / GeoGuessr 链接（混合模式下自动识别）")
            while True:
                user_input = input("\n请输入游戏ID或链接 (或输入 'q' 退出): ").strip()
                if user_input.lower() == "q":
                    break
                if not user_input:
                    continue
                try:
                    run_detection(backend, cookies, args, user_input)
                except (TuxunAPIError, GeoGuessrError, AIError) as exc:
                    print(f"出错: {exc}")
    except KeyboardInterrupt:
        print("\n程序已退出。")


if __name__ == "__main__":
    main()
