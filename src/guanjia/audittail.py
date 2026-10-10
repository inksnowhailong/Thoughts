"""跟随分级插件的审计文件（workspace\\guanjia\\audit.jsonl），把主线会话的工具调用实时交给面板。

网关的流式回复里没有内部工具的进度，所以从审计里拿：每次工具调用分级插件都写一行
{"ts", "tool", "level", "decision", "sessionKey", "summary", ...}。启动时文件已存在就从末尾开始，只看新写入的行；
启动时还没有这个文件、后来被换成新文件、或被截短，都从新内容的开头读。
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


def tool_entry(raw: object) -> dict | None:
    """一行审计 → 面板用的工具记录；不是工具调用、或不是主线会话的返回 None。内层去重那行也跳过（外层已经记过）。"""
    if not isinstance(raw, dict) or raw.get("kind") != "tool" or raw.get("sessionKey") != MAIN_SESSION_KEY \
            or not raw.get("tool"):
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
        self._pos: int | None = None  # None = 还没定位过
        self._ino: int | None = None  # 文件身份：被换成新文件时从头读
        self._buf = b""
        self._stop = threading.Event()

    def poll(self) -> int:
        """读一次新增内容，返回交给回调的条数。第一次调用时文件已存在就只定位到末尾。"""
        try:
            st = self.path.stat()
        except OSError:
            if self._pos is None:
                self._pos = 0  # 启动时还没有文件：以后出现的内容都是新的
            return 0
        if self._pos is None:
            self._pos, self._ino = st.st_size, st.st_ino
            return 0
        if st.st_ino != self._ino or st.st_size < self._pos:  # 换了文件 / 被截短
            self._pos, self._ino, self._buf = 0, st.st_ino, b""
        if st.st_size == self._pos:
            return 0
        try:
            with self.path.open("rb") as f:
                f.seek(self._pos)
                chunk = f.read(st.st_size - self._pos)
        except OSError:
            return 0  # 被占着：下次再读
        self._pos += len(chunk)
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
            try:
                self.poll()
            except Exception:  # noqa: BLE001  跟随出错不能让面板从此看不到工具调用
                log.exception("audit tail poll error")

    def stop(self) -> None:
        self._stop.set()
