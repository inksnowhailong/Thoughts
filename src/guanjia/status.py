"""守护进程的健康检查与托盘状态（纯逻辑，可单测；画图标在 tray.py）。"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

log = logging.getLogger("guanjia.status")

DISPLAY_NAME = "思绪"  # 二代的显示名（托盘提示、气泡标题）；唤醒词仍是「管家」，与它无关

SLEEP_LABELS = {"sleeping": "休眠中", "consolidating": "整理记忆中"}  # P4 休眠状态位的托盘文字
COLORS = {  # 托盘图标颜色（RGB）
    "listening": (46, 160, 67),
    "busy": (31, 111, 235),
    "muted": (140, 140, 140),
    "gaming": (140, 140, 140),
    "error": (218, 54, 51),
    "starting": (210, 153, 34),
    "pending": (163, 113, 247),  # 有一件事在等主人口头确认：紫色，和红（出错）、黄（启动中）都分得开
}


@dataclass(frozen=True)
class Health:
    """依赖是否在线；None = 还没查过。"""
    gateway: bool | None = None
    tts: bool | None = None


def tcp_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    """能否连上 TCP 端口（网关在线检查用）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class HealthMonitor:
    """后台线程每 interval 秒查一次各依赖，结果变化时回调 on_change(Health)。checks 的键只认 gateway / tts。"""

    def __init__(self, checks: dict[str, Callable[[], bool]], on_change: Callable[[Health], None],
                 interval: float = 10.0) -> None:
        self.checks = checks
        self.on_change = on_change
        self.interval = interval
        self.health = Health()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> Health:
        vals: dict[str, bool] = {}
        for name, fn in self.checks.items():
            try:
                vals[name] = bool(fn())
            except Exception:  # noqa: BLE001  检查本身出错就当不在线
                vals[name] = False
        new = replace(self.health, **vals)
        if new != self.health:
            self.health = new
            try:
                self.on_change(new)
            except Exception:  # noqa: BLE001
                log.exception("health on_change error")
        return new

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="health", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            self.check_once()
            if self._stop.wait(self.interval):
                return

    def stop(self) -> None:
        self._stop.set()


@dataclass(frozen=True)
class TrayView:
    key: str
    label: str
    color: tuple[int, int, int]
    tooltip: str


def _mark(v: bool | None) -> str:
    return "✓" if v else ("…" if v is None else "✗")


def tray_view(*, muted: bool, gaming: bool, busy: bool, health: Health, last_error: str = "",
              sleep_state: str = "awake", pending: str = "") -> TrayView:
    """按优先级 出错 > 待确认 > 静音 > 游戏中 > 启动中 > 说话中 > 在听 给出托盘状态。
    待确认（pending = 在等主人确认的那件事）压过静音和游戏中：打游戏时看不到气泡，图标变色是唯一的提醒。"""
    if health.gateway is False:
        key, label = "error", "出错：网关不在线"
    elif health.tts is False:
        key, label = "error", "出错：TTS 不在线"
    elif last_error:
        key, label = "error", f"出错：{last_error}"
    elif pending:
        key, label = "pending", f"等你确认：{pending[:40]}"
    elif muted:
        key, label = "muted", "静音（不听不说）"
    elif gaming:
        key, label = "gaming", "游戏中（不听不说）"
    elif health.gateway is None or health.tts is None:
        key, label = "starting", "启动中"
    elif busy:
        key, label = "busy", "说话中"
    else:
        key, label = "listening", "在听"
    tip = f"{DISPLAY_NAME}：{label}｜网关{_mark(health.gateway)} TTS{_mark(health.tts)}"
    if sleep_state in SLEEP_LABELS:
        tip += f"｜{SLEEP_LABELS[sleep_state]}"
    return TrayView(key, label, COLORS[key], tip[:120])  # Windows 托盘提示上限 128 字符
