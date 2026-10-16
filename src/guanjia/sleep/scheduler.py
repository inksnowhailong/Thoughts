"""P4 休眠演练调度：只在可靠主线水位确认空闲时触发 staging。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from guanjia.sleep.rehearsal import IDLE_SECONDS, SleepRehearsal


class RehearsalScheduler:
    """默认关闭；没有全主线实时水位时 fail-closed，不把“只看到语音”当安全。"""

    def __init__(self, rehearsal: SleepRehearsal, *, watermark: Callable[[], float | None],
                 watermark_reliable: Callable[[], bool], emit: Callable[[str, str, dict[str, Any]], None] | None = None,
                 enabled: bool = False, interval: float = 60.0, clock: Callable[[], float] = time.time) -> None:
        self.rehearsal, self.watermark, self.watermark_reliable = rehearsal, watermark, watermark_reliable
        self.emit, self.enabled, self.interval, self.clock = emit or (lambda *_: None), enabled, interval, clock
        self._stop, self._thread, self._lock = threading.Event(), None, threading.RLock()
        self._attempted_at: float | None = None
        self._state, self._reason = ("disabled", "演练开关关闭") if not enabled else ("blocked", "实时主线消息水位未接入")
        self._last_result: dict[str, Any] | None = None

    def note_activity(self, _at: float | None = None) -> None:
        """供未来可靠主线 activity feed 调用；当前语音事件只作诊断，不能解除 fail-closed。"""
        with self._lock:
            self._attempted_at = None
            self._state, self._reason = "watching", "检测到主线活动，重新计算空闲"

    def tick(self) -> None:
        with self._lock:
            if not self.enabled:
                return
            if not self.watermark_reliable():
                self._state, self._reason = "blocked", "实时主线消息水位复核尚未接入"
                self.emit("sleep.rehearsal_blocked", self._reason, {})
                return
            last = self.watermark()
            if last is None:
                self._state, self._reason = "blocked", "主线消息水位为空"
                return
            now = self.clock()
            if now - last < IDLE_SECONDS:
                self._attempted_at = None
                self._state, self._reason = "watching", "主线未连续空闲 60 分钟"
                return
            if self._attempted_at == last:
                return
            self._attempted_at = last
            self._state, self._reason = "running", "开始只读演练"
        try:
            result = self.rehearsal.run(last_message_at=last)
            data = {"status": result.status, "directory": str(result.directory) if result.directory else None,
                    "session_key": result.session_key or None, "limitation": "实时消息水位复核尚未接入"}
            with self._lock:
                self._state, self._reason, self._last_result = result.status, result.reason, data
            self.emit("sleep.rehearsal_result", result.reason, data)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._state, self._reason = "failed", f"{type(exc).__name__}: {exc}"
            self.emit("sleep.rehearsal_failed", self._reason, {})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled, "state": self._state, "reason": self._reason,
                    "idle_required_sec": IDLE_SECONDS, "watermark_reliable": self.watermark_reliable(),
                    "last_result": self._last_result}

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="sleep-rehearsal", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()

    def stop(self) -> None:
        self._stop.set()
