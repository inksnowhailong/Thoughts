"""跟随分级插件的审计文件（workspace\\guanjia\\audit.jsonl），把主线会话的工具调用实时交给面板。

网关的流式回复里没有内部工具的进度，所以从审计里拿：每次工具调用分级插件都写一行
{"ts", "tool", "level", "decision", "sessionKey", "summary", ...}。启动时从文件末尾开始，只看新写入的行。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("guanjia.audittail")

AUDIT_PATH = Path.home() / ".openclaw" / "workspace" / "guanjia" / "audit.jsonl"
MAIN_SESSION_KEY = "agent:main:main"

DECISION_STATE = {  # 审计 decision → 面板「工具/审批」方框的状态
    "allow": "done", "allow-confirmed": "done", "allow-inner-dedup": "done", "approval-allow-once": "done",
    "approval-requested": "active", "block-need-confirm": "blocked", "block-no-confirmer": "blocked",
    "approval-deny": "blocked", "approval-timeout": "blocked", "approval-cancelled": "blocked",
}


def tool_entry(raw: dict) -> dict | None:
    """一行审计 → 面板用的工具记录；不是工具调用、或不是主线会话的返回 None。内层去重那行也跳过（外层已经记过）。"""
    if raw.get("kind") != "tool" or raw.get("sessionKey") != MAIN_SESSION_KEY or not raw.get("tool"):
        return None
    decision = str(raw.get("decision", ""))
    if decision == "allow-inner-dedup":
        return None
    return {"ts": raw.get("ts", ""), "tool": raw["tool"], "level": raw.get("level", ""), "decision": decision,
            "state": DECISION_STATE.get(decision, "info"), "summary": str(raw.get("summary", ""))[:160]}


class AuditTail:
    """后台线程每 0.5 秒读一次新增行，主线会话的工具调用回调 on_tool(entry)。"""

    def __init__(self, on_tool: Callable[[dict], None], path: Path = AUDIT_PATH, interval: float = 0.5) -> None:
        self.on_tool = on_tool
        self.path = path
        self.interval = interval
        self._pos: int | None = None
        self._buf = b""
        self._stop = threading.Event()

    def poll(self) -> int:
        """读一次新增内容，返回交给回调的条数。第一次调用只定位到文件末尾。"""
        try:
            size = self.path.stat().st_size
        except OSError:
            return 0
        if self._pos is None or size < self._pos:  # 第一次，或文件被截断/轮转
            self._pos, self._buf = size, b""
            return 0
        if size == self._pos:
            return 0
        with self.path.open("rb") as f:
            f.seek(self._pos)
            chunk = f.read(size - self._pos)
        self._pos = size
        lines = (self._buf + chunk).split(b"\n")
        self._buf = lines.pop()  # 最后一段可能是写了一半的行
        n = 0
        for line in lines:
            try:
                entry = tool_entry(json.loads(line.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                continue
            if entry:
                n += 1
                try:
                    self.on_tool(entry)
                except Exception:  # noqa: BLE001
                    log.exception("audit tail callback error")
        return n

    def start(self) -> None:
        self.poll()
        threading.Thread(target=self._loop, name="audit-tail", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.poll()

    def stop(self) -> None:
        self._stop.set()
