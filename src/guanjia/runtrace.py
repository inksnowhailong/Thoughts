"""从网关的会话库里查一轮的阶段时间点，把"网关首字"拆成"网关准备"和"模型首字"（面板的耗时拆分用）。

只读打开 C:\\Users\\inksn\\.openclaw\\agents\\main\\agent\\openclaw-agent.sqlite 的 trajectory_runtime_events
（OpenClaw 2026.9.6 的内部表，版本已锁定）；表结构对不上或读失败就返回 None，面板只显示合并后的"网关首字"。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

AGENT_DB = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
_PHASES = {"session.started": "session_started", "prompt.submitted": "prompt_submitted",
           "model.completed": "model_completed"}


def run_phases(run_id: str, db: Path = AGENT_DB) -> dict | None:
    """返回 {session_started, prompt_submitted, model_completed, first_tool}（epoch 秒），查不到返回 None。"""
    if not run_id or not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute("select event_json, created_at from trajectory_runtime_events where run_id = ? order by seq",
                               (run_id,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    out: dict[str, float] = {}
    for event_json, created_at in rows:
        try:
            kind = json.loads(event_json).get("type")
        except (ValueError, TypeError):
            continue
        key = _PHASES.get(kind) or ("first_tool" if kind == "tool.call" else None)
        if key and key not in out:
            out[key] = float(created_at) / 1000.0
    return out or None
