#!/usr/bin/env python3
"""tuxun_tui.py — 图寻助手 · TUI 日志后台（--tui）。

用 Rich 在终端里渲染一个实时仪表盘：
  * 端口状态（本地代理 / 图寻镜像 / GeoGuessr 镜像 / 控制 API）
  * 捕获状态（原点 / 目前 / 答案 / 诱饵 / 候选）
  * 最近日志（logs/proxy.log 尾部，Cookie 等敏感信息已被 applog 脱敏）

依赖：rich（pip install rich；缺失时自动降级为纯文本轮询输出）。
按 Ctrl+C 退出（系统代理会自动还原）。
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅为类型标注，避免运行时依赖
    from tuxun_proxy import TuxunApp

try:
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    RICH_AVAILABLE = False

REFRESH_SEC = 1.0
LOG_TAIL_LINES = 14


def _tail(path: str, n: int = LOG_TAIL_LINES) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip() for ln in f.readlines()[-n:]]
    except OSError:
        return []


def _state_snapshot(app: "TuxunApp") -> dict:
    origin = app._round_anchor
    with app._lock:
        cur = dict(app._cur_pos) if app._cur_pos else None
        ans = dict(app._last_answer) if app._last_answer else None
        decoys = sum(1 for r in app._history if r.get("decoy"))
        cands = sum(1 for r in app._history if r.get("candidate"))
    return {
        "origin": origin[0] if origin else None,
        "origin_trusted": bool(origin and origin[3]),
        "current": cur,
        "answer": ans,
        "round_move": app._round_move,
        "decoys": decoys,
        "candidates": cands,
    }


def _fmt_point(p) -> str:
    if not p:
        return "-"
    return f"{p[0]:.5f}, {p[1]:.5f}"


def _port_text(label: str, host_port, running: bool) -> Text:
    mark = "[green]●[/green]" if running else "[red]○[/red]"
    port = f"127.0.0.1:{host_port}" if host_port else "-"
    return Text.from_markup(f" {mark} {label:<14} {port}")


def build_renderable(app: "TuxunApp", mirrors: dict, log_file: str):
    st = _state_snapshot(app)
    cfg = app.config

    ports = Table.grid(padding=(0, 2))
    ports.add_row(_port_text("本地代理", app.port, app.server.running))
    ports.add_row(_port_text("图寻镜像", mirrors.get("tuxun"), mirrors.get("tuxun")))
    ports.add_row(_port_text("Geo镜像", mirrors.get("geoguessr"), mirrors.get("geoguessr")))
    ports.add_row(_port_text("控制/选择页", cfg.get("control_port", 18080), True))

    info = Table.grid(padding=(0, 2))
    info.add_row(Text.from_markup(
        f" 原点 [yellow]{_fmt_point(st['origin'])}[/yellow]"
        f"  目前 [green]{_fmt_point(st['current'])}[/green]"
        f"  答案 [cyan]{_fmt_point(st['answer'])}[/cyan]"))
    info.add_row(Text.from_markup(
        f" 诱饵 [red]{st['decoys']}[/red]  候选 [orange3]{st['candidates']}[/orange3]"
        f"  模式 {st['round_move'] if st['round_move'] is not None else '-'}"
        f"  API直读 {'开' if cfg.get('api_poll') else '关'}"
        f"  一键分数 {'开(' + str(cfg.get('oneclock_score')) + '/' + str(cfg.get('oneclock_key')) + ')' if cfg.get('oneclock_enabled') else '关'}"))

    logs = Text("\n".join(_tail(log_file)) or "（暂无日志）", style="dim", overflow="fold")

    return Group(
        Panel(ports, title="端口", expand=False),
        Panel(info, title="捕获状态", expand=False),
        Panel(logs, title="日志（尾部）", expand=False),
        Text.from_markup(" [dim]Ctrl+C 退出（系统代理自动还原）｜网页控制台: "
                         f"http://127.0.0.1:{cfg.get('control_port', 18080)}/[/dim]"),
    )


def run_tui(app: "TuxunApp", mirrors: dict, log_file: str) -> None:
    """阻塞运行 TUI，直到 Ctrl+C。"""
    if not RICH_AVAILABLE:
        print("[TUI] 未安装 rich，降级为简单输出。可执行: pip install rich")
        try:
            while True:
                st = _state_snapshot(app)
                print(f"[{time.strftime('%H:%M:%S')}] 原点={_fmt_point(st['origin'])} "
                      f"目前={_fmt_point(st['current'])} 答案={_fmt_point(st['answer'])} "
                      f"诱饵={st['decoys']} 候选={st['candidates']}")
                time.sleep(REFRESH_SEC)
        except KeyboardInterrupt:
            return
    from rich.console import Console

    console = Console()
    try:
        with Live(build_renderable(app, mirrors, log_file), console=console,
                  refresh_per_second=2, screen=False) as live:
            while True:
                time.sleep(REFRESH_SEC)
                live.update(build_renderable(app, mirrors, log_file))
    except KeyboardInterrupt:
        return
