"""统一日志配置。

控制台彩色输出 + 按天滚动的文件日志。Web 端可以通过 attach_sink 挂一个
回调进来, 把日志实时推给前端 SSE。
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Callable

from .config import LOGS_DIR

_CONFIGURED = False

_COLORS = {
    "DEBUG": "\033[38;5;244m",
    "INFO": "\033[38;5;39m",
    "WARNING": "\033[38;5;214m",
    "ERROR": "\033[38;5;203m",
    "CRITICAL": "\033[48;5;203;38;5;231m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        record.short_name = record.name.replace("gaj.", "")
        base = super().format(record)
        if color and sys.stderr.isatty():
            return f"{color}{base}{_RESET}"
        return base


class _SinkHandler(logging.Handler):
    """把日志转发给外部回调 (给 Web SSE 用)。"""

    def __init__(self) -> None:
        super().__init__()
        self._sinks: list[Callable[[dict], None]] = []

    def add(self, fn: Callable[[dict], None]) -> None:
        self._sinks.append(fn)

    def remove(self, fn: Callable[[dict], None]) -> None:
        if fn in self._sinks:
            self._sinks.remove(fn)

    def emit(self, record: logging.LogRecord) -> None:
        if not self._sinks:
            return
        payload = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name.replace("gaj.", ""),
            "message": record.getMessage(),
        }
        for fn in list(self._sinks):
            try:
                fn(payload)
            except Exception:
                pass


SINK = _SinkHandler()


def setup(level: int = logging.INFO, quiet_console: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("gaj")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = "%(asctime)s %(levelname)-7s [%(short_name)s] %(message)s"
    datefmt = "%H:%M:%S"

    if not quiet_console:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        console.setFormatter(_ColorFormatter(fmt, datefmt))
        root.addHandler(console)

    logfile = LOGS_DIR / f"gaj-{time.strftime('%Y-%m-%d')}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(fh)

    SINK.setLevel(logging.INFO)
    root.addHandler(SINK)


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"gaj.{name}")
