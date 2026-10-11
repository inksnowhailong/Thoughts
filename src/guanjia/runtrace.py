"""从网关的会话库里查一轮的阶段时间点，把"网关首字"拆成"网关准备"和"模型首字"（面板的耗时拆分用）；run_summary 还摘出模型、stop、token、每次工具调用的成败（统一事件日志的 gateway.run 用）。

只读打开 C:\\Users\\inksn\\.openclaw\\agents\\main\\agent\\openclaw-agent.sqlite 的 trajectory_runtime_events
（OpenClaw 2026.9.6 的内部表，版本已锁定）；表结构对不上或读失败就返回 None，面板只显示合并后的"网关首字"。
绝不写它：网关正在写这个库，我们只用 mode=ro 读，不能锁住网关。
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


# ---------- 统一事件日志用：一个 run 的完整摘要 ----------
TRACE_SCHEMA = "openclaw-trajectory"  # OpenClaw 2026.9.6 trajectory schemaVersion 1；对不上就返回 None
WRAPPER_TOOL = "tool_call"  # tool_search 代码模式下，真实工具外面还套一条 name=tool_call 的包装调用


def _connect(db: Path) -> sqlite3.Connection | None:
    """只读连接（mode=ro）：库不存在 / 打不开返回 None。"""
    if not db.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _rows(run_id: str, db: Path) -> list[tuple[str, int]]:
    con = _connect(db) if run_id else None
    if con is None:
        return []
    try:
        return con.execute("select event_json, created_at from trajectory_runtime_events where run_id = ? order by seq",
                           (run_id,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _brief(v, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _error_text(res) -> str:
    """工具失败时给人看的一句：结果正文第一段，没有就退到 details.error。"""
    content = res.get("content") if isinstance(res, dict) else None
    if isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("text"):
        return _brief(content[0]["text"], 200)
    det = res.get("details") if isinstance(res, dict) else None
    if isinstance(det, dict) and det.get("error"):
        return _brief(det["error"], 200)
    return _brief(res, 200)


def _collapse_wrappers(calls: dict[str, dict]) -> list[dict]:
    """去掉 tool_search 代码模式的外层包装调用：有内层真实调用（toolCallId 含外层 id）就丢外层，
    没有内层（还没跑就失败）就保留，名字换成包装里写的真实工具名。"""
    out = []
    for cid, c in calls.items():
        if c.get("name") == WRAPPER_TOOL:
            if any(o != cid and cid in o for o in calls):
                continue
            real = (c.pop("wrapped", None) or "").split(":")[-1]
            if real:
                c["name"] = real
        c.pop("wrapped", None)
        c.pop("t", None)
        out.append(c)
    return out


def run_summary(run_id: str, db: Path = AGENT_DB) -> dict | None:
    """一个 run 的摘要：模型、stop、usage、每次工具调用（成败/耗时/参数/错误）、阶段时间点、发给模型的上下文摘要。
    model.completed 还没写出来（轮次刚结束）、traceSchema 对不上、表读不了时返回 None，调用方稍后重试。"""
    out: dict = {"run_id": run_id, "phases": {}, "tools": []}
    calls: dict[str, dict] = {}
    done = False
    for event_json, created_at in _rows(run_id, db):
        try:
            e = json.loads(event_json)
        except (ValueError, TypeError):
            continue
        if e.get("traceSchema") not in (None, TRACE_SCHEMA):
            return None
        kind, d, t = e.get("type"), e.get("data") or {}, float(created_at) / 1000.0
        key = _PHASES.get(kind) or ("first_tool" if kind == "tool.call" else None)
        if key and key not in out["phases"]:
            out["phases"][key] = t
        if kind == "session.started":
            out.update(provider=e.get("provider"), model=e.get("modelId"), trigger=d.get("trigger"),
                       channel=d.get("messageProvider"), tool_count=d.get("toolCount"), session_key=e.get("sessionKey"))
        elif kind == "context.compiled":
            msgs = d.get("messages")
            msgs = msgs if isinstance(msgs, list) else []
            sp = d.get("systemPrompt")
            tools = d.get("tools")
            out["ctx"] = {"prompt": d.get("prompt"), "messages_total": len(msgs), "messages_tail": msgs[-6:],
                          "system_chars": sp.get("originalChars") if isinstance(sp, dict) else len(sp or ""),
                          "tools": [x.get("name") if isinstance(x, dict) else str(x) for x in tools][:80] if isinstance(tools, list) else []}
        elif kind == "tool.call":
            cid = d.get("toolCallId") or f"#{len(calls)}"
            args = d.get("args")
            wrapped = args.get("id") if d.get("name") == WRAPPER_TOOL and isinstance(args, dict) else None
            calls[cid] = {"name": d.get("name"), "t": t, "args": _brief(args), "wrapped": wrapped}
        elif kind == "tool.result":
            c = calls.setdefault(d.get("toolCallId") or f"#{len(calls)}", {"name": d.get("name"), "t": t})
            res = d.get("result") or {}
            det = res.get("details") if isinstance(res, dict) else None
            det = det if isinstance(det, dict) else {}
            c.update(ok=bool(d.get("success")), ms=det.get("durationMs") or round((t - c["t"]) * 1000), exit=det.get("exitCode"))
            if not d.get("success"):
                c["err"] = _error_text(res)
        elif kind == "model.completed":
            done = True
            out.setdefault("stop", d.get("stopReason"))
            out.setdefault("usage", d.get("usage"))
        elif kind == "trace.artifacts":
            u = d.get("usage") or {}
            out.update(stop=d.get("stopReason"), final_status=d.get("finalStatus"), aborted=d.get("aborted"),
                       timed_out=d.get("timedOut"), prompt_error=d.get("promptErrorSource"),
                       usage={k: u.get(k) for k in ("input", "output", "cacheRead", "cacheWrite", "total")},
                       texts=[_brief(x, 400) for x in (d.get("assistantTexts") or [])])
    if not done:
        return None
    out["tools"] = _collapse_wrappers(calls)
    return out


def recent_runs(n: int, db: Path = AGENT_DB) -> list[str]:
    """最近 n 个 run_id（新到旧）。"""
    con = _connect(db)
    if con is None:
        return []
    try:
        rows = con.execute("select run_id, max(rowid) m from trajectory_runtime_events where run_id is not null "
                           "group by run_id order by m desc limit ?", (n,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [r[0] for r in rows]