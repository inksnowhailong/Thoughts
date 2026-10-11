"""runtrace.run_summary / recent_runs：从网关 trajectory 表摘要一个 run（模型、stop、usage、每次工具调用成败、阶段时间点、上下文摘要）。
临时 sqlite，结构照 2026-10-11 在家里电脑上实测的 OpenClaw 2026.9.6（每个事件顶层带 runId/provider/modelId，
tool_search 代码模式下会有 name=tool_call 的外层包装 + 内层真实工具两条调用）。只读打开，不碰线上库。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_runtrace_summary.py
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from guanjia import runtrace


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def make_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("create table trajectory_runtime_events (session_id text not null, seq integer not null, run_id text, "
                "event_json text not null, created_at integer not null, primary key (session_id, seq))")
    return con


SEQ = [0]


def add(con, run_id, typ, data, t_ms, **top):
    SEQ[0] += 1
    e = {"traceSchema": "openclaw-trajectory", "schemaVersion": 1, "type": typ, "runId": run_id, "provider": "siliconflow",
         "modelId": "deepseek-ai/DeepSeek-V4-Flash", "data": data, **top}
    con.execute("insert into trajectory_runtime_events values (?,?,?,?,?)", ("s1", SEQ[0], run_id, json.dumps(e, ensure_ascii=False), t_ms))


def full_run(con, run_id, t0=1_791_698_000_000, *, with_done=True, schema=None):
    kw = {"traceSchema": schema} if schema else {}
    add(con, run_id, "session.started", {"trigger": "user", "messageProvider": "webchat", "toolCount": 12}, t0, **kw)
    add(con, run_id, "context.compiled", {
        "systemPrompt": {"truncated": True, "originalChars": 47399, "limitChars": 32768},
        "prompt": "[语音 2026-10-11 13:57 周日] 现在就赶紧给我下载安装",
        "messages": [{"role": "user", "content": f"m{i}"} for i in range(10)],
        "tools": [{"name": "exec"}, {"name": "web_fetch"}, {"name": "speak"}]}, t0 + 30, **kw)
    add(con, run_id, "prompt.submitted", {"prompt": "x"}, t0 + 40, **kw)
    add(con, run_id, "tool.call", {"toolCallId": "c1", "name": "exec", "args": {"command": "Get-PnpDevice " + "x" * 400}}, t0 + 1000, **kw)
    add(con, run_id, "tool.call", {"toolCallId": "c2", "name": "web_fetch", "args": {"url": "https://example.com"}}, t0 + 1100, **kw)
    add(con, run_id, "tool.result", {"toolCallId": "c1", "name": "exec", "success": True,
                                     "result": {"content": [{"type": "text", "text": "ok"}], "details": {"durationMs": 850, "exitCode": 0}}}, t0 + 1900, **kw)
    add(con, run_id, "tool.result", {"toolCallId": "c2", "name": "web_fetch", "success": False,
                                     "result": {"content": [{"type": "text", "text": "blocked by guard"}], "details": {"status": "error"}}}, t0 + 1500, **kw)
    if with_done:
        add(con, run_id, "model.completed", {"stopReason": "stop", "usage": {"input": 10, "output": 5}}, t0 + 3000, **kw)
        add(con, run_id, "trace.artifacts", {
            "finalStatus": "success", "aborted": False, "timedOut": False, "promptErrorSource": None, "stopReason": "stop",
            "usage": {"input": 2104, "output": 178, "cacheRead": 58112, "cacheWrite": 0, "total": 60394, "cost": {"total": 0}},
            "assistantTexts": ["我先用只读方式找一下"]}, t0 + 3001, **kw)
        add(con, run_id, "session.ended", {"status": "success"}, t0 + 3030, **kw)


with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "agent.sqlite"
    con = make_db(db)
    full_run(con, "chatcmpl_aaa")
    con.commit()
    s = runtrace.run_summary("chatcmpl_aaa", db)
    check(s is not None and s["model"] == "deepseek-ai/DeepSeek-V4-Flash" and s["provider"] == "siliconflow", "model / provider from the event top level")
    check(s["stop"] == "stop" and s["final_status"] == "success" and s["usage"]["input"] == 2104 and s["usage"]["cacheRead"] == 58112,
          "stop / final_status / usage come from trace.artifacts")
    check(s["channel"] == "webchat" and s["tool_count"] == 12 and s["aborted"] is False, "channel, toolCount, aborted")
    check(len(s["tools"]) == 2 and s["tools"][0]["name"] == "exec" and s["tools"][0]["ok"] is True and s["tools"][0]["ms"] == 850
          and s["tools"][0]["exit"] == 0, "first tool: ok, duration and exit code from details")
    check(s["tools"][1]["ok"] is False and "blocked" in s["tools"][1]["err"], "failed tool carries the error text")
    check(len(s["tools"][0]["args"]) <= 164 and s["tools"][0]["args"].endswith("…"), "tool args are clipped")
    check(len(s["ctx"]["messages_tail"]) == 6 and s["ctx"]["messages_total"] == 10 and s["ctx"]["system_chars"] == 47399
          and s["ctx"]["tools"] == ["exec", "web_fetch", "speak"], "ctx: last 6 messages, system chars from originalChars, tool names")
    check(s["texts"] == ["我先用只读方式找一下"] and s["ctx"]["prompt"].startswith("[语音"), "assistant texts and prompt")
    ph = s["phases"]
    check(abs(ph["session_started"] - 1_791_698_000.0) < 1e-6 and ph["first_tool"] > ph["prompt_submitted"] and ph["model_completed"] > ph["first_tool"],
          "phases are epoch seconds in order")

    # 没有 model.completed（轮次刚结束 trajectory 还没写完）→ None，调用方稍后重试
    full_run(con, "chatcmpl_bbb", with_done=False)
    con.commit()
    check(runtrace.run_summary("chatcmpl_bbb", db) is None, "no model.completed yet -> None")

    # schema 对不上 → None
    full_run(con, "chatcmpl_ccc", schema="something-else")
    con.commit()
    check(runtrace.run_summary("chatcmpl_ccc", db) is None, "unknown traceSchema -> None")

    # tool_search 代码模式：外层 tool_call 包装 + 内层真实工具 → 只算一次
    t0 = 1_791_699_000_000
    add(con, "chatcmpl_ddd", "session.started", {"messageProvider": "webchat"}, t0)
    add(con, "chatcmpl_ddd", "tool.call", {"toolCallId": "w1", "name": "tool_call", "args": {"id": "openclaw:core:web_fetch", "args": {"url": "u"}}}, t0 + 10)
    add(con, "chatcmpl_ddd", "tool.call", {"toolCallId": "tool_search_code:w1:web_fetch:", "name": "web_fetch", "args": {"url": "u"}}, t0 + 20)
    add(con, "chatcmpl_ddd", "tool.result", {"toolCallId": "tool_search_code:w1:web_fetch:", "name": "web_fetch", "success": False,
                                             "result": {"content": [], "details": {"status": "error", "error": "404 not found"}}}, t0 + 90)
    add(con, "chatcmpl_ddd", "tool.result", {"toolCallId": "w1", "name": "tool_call", "success": False,
                                             "result": {"content": [{"type": "text", "text": "{\"error\":\"404 not found\"}"}], "details": {"status": "error"}}}, t0 + 100)
    # 只有外层包装、没有内层（例如还没跑就失败）→ 保留，名字换成包装里的真实工具名
    add(con, "chatcmpl_ddd", "tool.call", {"toolCallId": "w2", "name": "tool_call", "args": {"id": "openclaw:core:exec", "args": {}}}, t0 + 110)
    add(con, "chatcmpl_ddd", "tool.result", {"toolCallId": "w2", "name": "tool_call", "success": False,
                                             "result": {"content": [{"type": "text", "text": "denied"}], "details": {}}}, t0 + 120)
    add(con, "chatcmpl_ddd", "model.completed", {"stopReason": "stop"}, t0 + 200)
    con.commit()
    s = runtrace.run_summary("chatcmpl_ddd", db)
    check(s is not None and [x["name"] for x in s["tools"]] == ["web_fetch", "exec"], f"wrapper deduped: {[x['name'] for x in s['tools']]}")
    check(s["tools"][0]["ok"] is False and "404" in s["tools"][0]["err"], "inner tool error falls back to details.error")
    check(s["stop"] == "stop" and s["usage"] is None, "model.completed alone still gives stop (no artifacts)")

    # recent_runs
    check(runtrace.recent_runs(1, db) == ["chatcmpl_ddd"] and runtrace.recent_runs(2, db) == ["chatcmpl_ddd", "chatcmpl_ccc"],
          "recent_runs newest first")
    con.close()

    # 表不存在 / 库不存在 → None / [] 不抛
    empty = Path(tmp) / "empty.sqlite"
    sqlite3.connect(empty).close()
    check(runtrace.run_summary("x", empty) is None and runtrace.recent_runs(3, empty) == [], "missing table -> None / []")
    check(runtrace.run_summary("x", Path(tmp) / "nope.sqlite") is None and runtrace.recent_runs(3, Path(tmp) / "nope.sqlite") == [], "missing db -> None / []")
    check(runtrace.run_summary("", db) is None, "empty run id -> None")

    # 只读：查询不会改库
    before = db.stat().st_size, db.read_bytes()
    runtrace.run_summary("chatcmpl_aaa", db)
    check((db.stat().st_size, db.read_bytes()) == before, "read-only: the db file is untouched")

    # 旧接口保持不变
    ph = runtrace.run_phases("chatcmpl_aaa", db)
    check(ph is not None and set(ph) == {"session_started", "prompt_submitted", "model_completed", "first_tool"}, "run_phases signature and result unchanged")

print("ALL PASS test_runtrace_summary")
