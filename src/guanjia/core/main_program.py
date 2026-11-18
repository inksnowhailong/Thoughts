"""Complete-turn main program boundary（ModularBrain 的效果账本提交口）。

ModularBrain（voice.py 生产主线）把每轮语音的效果流经 ProductionMainProgram
提交进 core 运行时账本（data/modular-main.sqlite + modular-effects.jsonl）。
影子模式（shadow）已于 2026-11-05 随 OpenClaw 退役整体删除——需要对比验证时用 git。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .agent_runtime import CommitPort, MainAgentRuntime, TurnOutcome


class TurnCancelledError(RuntimeError):
    """低优先级评估被新输入/打断取消。"""


class CancellationToken:
    """Thread-safe cancellation token with optional transport abort callbacks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cancelled = False
        self._reason: str | None = None
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = 'cancelled') -> None:
        if not isinstance(reason, str) or not reason.strip():
            reason = 'cancelled'
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason.strip()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass

    def on_cancel(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._cancelled:
                run_now = True
            else:
                self._callbacks.append(callback)
                run_now = False
        if run_now:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass

    def raise_if_cancelled(self) -> None:
        with self._lock:
            if self._cancelled:
                raise TurnCancelledError(self._reason or 'cancelled')


class ProductionMainProgram:
    """Production counterpart that commits real effects."""

    def __init__(self, *, commit: CommitPort, repairer: object | None = None) -> None:
        self._runtime = MainAgentRuntime(commit=commit, repairer=repairer)

    def process_events(self, events: object) -> TurnOutcome:
        """Commit a complete structured event batch to the production commit port."""
        return self._runtime.process_events(events)  # type: ignore[arg-type]

    def process_jsonl(self, source: str | bytes) -> TurnOutcome:
        """Commit complete JSONL emitted by a production generator."""
        return self._runtime.process_jsonl(source)


__all__ = ['CancellationToken', 'ProductionMainProgram', 'TurnCancelledError']
