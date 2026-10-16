"""主动播报的唯一决策点；不操作 TTS、托盘或任务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class AnnouncementDecision:
    """speak=True 才允许调用现有 TTS；否则调用方只保留托盘和结构化事件。"""

    speak: bool
    reason: str


class AnnouncementPolicy:
    """只读取 ProgramQuietMonitor 的稳定 quiet_active，不读取前台窗口或临时候选。"""

    def __init__(self, quiet_active: Callable[[], bool]) -> None:
        self._quiet_active = quiet_active

    def should_announce(self, event_kind: str, user_initiated: bool) -> AnnouncementDecision:
        """用户明确发起的正常回复永远可念；后台事件在影音静默时仅托盘/日志。"""
        if user_initiated:
            return AnnouncementDecision(True, "user_initiated")
        if self._quiet_active():
            return AnnouncementDecision(False, "program_quiet")
        return AnnouncementDecision(True, "normal")
