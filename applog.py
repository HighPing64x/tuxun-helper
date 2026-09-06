"""applog.py — 统一日志系统：滚动文件 + 自动脱敏。

* 日志文件位于 logs/ 目录（按入口分文件，避免多进程轮转冲突），
  超过 1MB 自动滚动，最多保留 3 份；
* 键名含 key/cookie/token/ticket/session/password/secret 的字段自动打码；
* 文本中疑似 Cookie/Key 的长串（如 fun_ticket=xxx）兜底打码；
* 任何敏感信息都不应写入日志——新增日志点时请用 sanitize/sanitize_json。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re

import sys

BASE_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)  # PyInstaller onefile：日志跟随 exe
    else os.path.dirname(os.path.abspath(__file__))
)
LOG_DIR = os.path.join(BASE_DIR, "logs")

_SENSITIVE_KEY = re.compile(
    r"key|cookie|token|ticket|session|password|passwd|secret|authorization|match|replace", re.I
)
_MAX_VALUE = 400


def mask_text(text: str) -> str:
    """兜底脱敏：把文本里疑似 Cookie/Key 的赋值片段打码。"""
    if not text:
        return text
    return re.sub(
        r"((?:fun_ticket|session|token|api[_-]?key|password|secret)\s*[=：]\s*)\S+",
        r"\1******",
        text,
        flags=re.I,
    )


def sanitize(data):
    """递归脱敏：dict/list 中键名敏感的字段替换为 ******，长文本截断。"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if _SENSITIVE_KEY.search(str(k)):
                out[k] = "******"
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(data, (list, tuple)):
        return [sanitize(x) for x in data]
    if isinstance(data, str):
        s = mask_text(data)
        return s[:_MAX_VALUE] + "…" if len(s) > _MAX_VALUE else s
    return data


def sanitize_json(data) -> str:
    """脱敏后序列化为单行 JSON（用于记录配置快照）。"""
    try:
        return json.dumps(sanitize(data), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(sanitize(data))


class _MaskedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """写入文件前强制脱敏（兜底：即使调用方忘记 sanitize 也不会泄露 Cookie/Key）。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            record.msg = mask_text(str(record.getMessage()))
            record.args = None
        except Exception:
            pass
        super().emit(record)


def setup(filename: str = "tuxun-helper", level: int = logging.INFO) -> str:
    """为本入口挂接滚动文件日志（幂等），返回日志文件路径。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"{filename}.log")
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _MaskedRotatingFileHandler) and getattr(h, "_tuxun", False):
            return log_file  # 已挂接，幂等
    fh = _MaskedRotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh._tuxun = True
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(fh)
    root.setLevel(level)
    return log_file
