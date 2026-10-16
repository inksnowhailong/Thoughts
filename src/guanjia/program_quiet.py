"""程序感知静默候选状态机；只产出状态，绝不触碰播放、麦克风或唤醒。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from guanjia.program_context import ForegroundApp, ProgramJudgement, classify_media, foreground_app, snapshot


NORMAL = "normal"
CANDIDATE_ENTER = "candidate_enter"
QUIET_ACTIVE = "quiet_active"
CANDIDATE_EXIT = "candidate_exit"


class ProgramQuietMonitor:
    """前台候选连续命中/消失后才切状态；探测失败立即 fail-open。"""

    def __init__(
        self,
        config: Callable[[], object],
        *,
        probe: Callable[[], ForegroundApp] = foreground_app,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[str, str, dict], None] | None = None,
        interval: float = 2.0,
    ) -> None:
        self._config, self._probe, self._clock, self._on_change, self.interval = config, probe, clock, on_change, interval
        self.state = NORMAL
        self._since = clock()
        self._app = ForegroundApp()
        self._judgement = ProgramJudgement(False, "disabled")
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def quiet_active(self) -> bool:
        # 退出滞后期间仍保持稳定 quiet-active，避免前台切换一帧就闪回正常。
        return self.state in {QUIET_ACTIVE, CANDIDATE_EXIT}

    def tick(self) -> None:
        """一次只读采样。Q1 仅更新状态，调用方不得据此压制语音。"""
        with self._lock:
            now, config = self._clock(), self._config()
            try:
                app = self._probe()
            except Exception as exc:  # noqa: BLE001
                app = ForegroundApp(error=f"probe:{type(exc).__name__}")
            judgement = classify_media(app, config)
            # 取不到前台窗口/程序名时立即退出候选，确保 fail-open。
            candidate = judgement.candidate and not (app.error and not app.exe_name)
            old = self.state
            if not candidate:
                if self.state == QUIET_ACTIVE:
                    self.state, self._since = CANDIDATE_EXIT, now
                elif self.state == CANDIDATE_EXIT:
                    if now - self._since >= getattr(config, "exit_hold_sec", 15.0):
                        self.state, self._since = NORMAL, now
                elif self.state != NORMAL:
                    self.state, self._since = NORMAL, now
            elif self.state == NORMAL:
                self.state, self._since = CANDIDATE_ENTER, now
            elif self.state == CANDIDATE_ENTER and now - self._since >= getattr(config, "enter_hold_sec", 5.0):
                self.state, self._since = QUIET_ACTIVE, now
            elif self.state == CANDIDATE_EXIT:
                self.state, self._since = QUIET_ACTIVE, now
            self._app, self._judgement = app, judgement
            if old != self.state and self._on_change is not None:
                self._on_change(old, self.state, self.snapshot(now))

    def snapshot(self, now: float | None = None) -> dict:
        with self._lock:
            now = self._clock() if now is None else now
            data = snapshot(self._app, self._judgement)
            data.update({"state": self.state, "quiet_active": self.quiet_active, "state_for_sec": round(max(0.0, now - self._since), 1)})
            return data

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="program-quiet", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 2)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()
