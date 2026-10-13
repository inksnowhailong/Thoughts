"""P4-0：提示词体积测量脚本（scripts/prompt_size.py）。只用临时 SQLite 和假网关，不碰线上。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p4_prompt_size.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import _p4_oc_fallback  # noqa: E402
import prompt_size  # noqa: E402

failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


KEY = "agent:main:exp-slim-test"
REPORT = {
    "systemPrompt": {"chars": 1000, "projectContextChars": 300, "nonProjectContextChars": 700},
    "injectedWorkspaceFiles": [
        {"name": "AGENTS.md", "path": "X:/ws/AGENTS.md", "injectedChars": 200},
        {"name": "USER.md", "path": "X:/ws/USER.md", "injectedChars": 100},
    ],
    "skills": {"promptChars": 250, "entries": [{"name": "a", "blockChars": 100}, {"name": "b", "blockChars": 150}]},
    "tools": {"entries": [
        {"name": "read", "summaryChars": 40, "schemaChars": 70},
        {"name": "exec", "summaryChars": 60, "schemaChars": 140},
    ]},
    "currentTurn": {"promptChars": 30, "runtimeContextChars": 70},
}
SNAPSHOT = {"prompt": "<available_skills>skill a skill b</available_skills>"}

tmp = Path(tempfile.mkdtemp(prefix="gj-p4size-"))
db = tmp / "agent.sqlite"
con = sqlite3.connect(db)
con.execute("create table session_nodes (session_key text primary key, entry_json text)")
con.execute("insert into session_nodes values (?, ?)",
            (KEY, json.dumps({"systemPromptReport": REPORT, "skillsSnapshot": SNAPSHOT})))
con.commit()
con.close()

# ---- report_for：汇总字段 ----
rep = prompt_size.report_for(KEY, db)
check(rep["system_chars"] == 1000 and rep["project_chars"] == 300 and rep["non_project_chars"] == 700, "system chars summed")
check(rep["skills"] == ["a", "b"] and rep["skills_chars"] == 250, "skill count and chars")
check(rep["tools"] == ["read", "exec"], "tool names")
check(rep["tool_summary_chars"] == 100 and rep["tool_schema_chars"] == 210, "tool summary/schema chars")
check(rep["files"] == {"AGENTS.md": 200, "USER.md": 100}, "injected files chars")
check(rep["turn_chars"] == 100, "current turn chars")
check(rep["skills_prompt"] == SNAPSHOT["prompt"], "skills prompt text kept for token estimate")
check(prompt_size.report_for("agent:main:nope", db) == {}, "missing session gives empty report")

# ---- breakdown：分项 token ----
bd = prompt_size.breakdown(rep, file_texts={"AGENTS.md": "中" * 145, "USER.md": "abcd" * 25})
rows = {r["part"]: r for r in bd["rows"]}
check(rows["AGENTS.md"]["tokens"] == 100 and rows["USER.md"]["tokens"] == 25, "file tokens use cjk/1.45 + other/4")
check(rows["OpenClaw 自带段落"]["chars"] == 700 - 250 - 100, "builtin = non-project - skills - tool summaries")
check(rows["技能列表"]["chars"] == 250 and rows["工具摘要"]["chars"] == 100 and rows["工具 schema"]["chars"] == 210, "group chars")
check(rows["本轮用户话+运行时"]["chars"] == 100, "turn row")
check(set(bd["skills"]) == {"a", "b"} and set(bd["tools"]) == {"read", "exec"}, "per-skill and per-tool detail")
check(bd["skills"]["a"] + bd["skills"]["b"] == rows["技能列表"]["tokens"] or abs(bd["skills"]["a"] + bd["skills"]["b"] - rows["技能列表"]["tokens"]) <= 1,
      "per-skill tokens add up to the skills row")
check(bd["total"] == sum(r["tokens"] for r in bd["rows"]), "total is the sum of rows")
bd2 = prompt_size.breakdown(rep, file_texts={})
check({r["part"]: r for r in bd2["rows"]}["AGENTS.md"]["tokens"] == 50, "no file text: fall back to chars/4")

# ---- 估算与中位数 ----
check(prompt_size.estimate_tokens("中中中中中中中中中中中中中中中中中中中中") == 14, "estimate_tokens cjk")
check(prompt_size.median([3, 1, 2]) == 2 and prompt_size.median([4, 2, 6, 8]) == 5, "median odd/even")

# ---- gateway_call 兜底实现：argv 直传 JSON，session-changed 映射 ----
calls = []


class FakeProc:
    def __init__(self, rc=0, out="{\"ok\": true}", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def fake_run(argv, **kw):
    calls.append(argv)
    return FakeProc(0, "banner line\n{\"ok\": true, \"n\": 1}")


real = _p4_oc_fallback.subprocess.run
_p4_oc_fallback.subprocess.run = fake_run
try:
    out = _p4_oc_fallback.gateway_call("sessions.delete", {"key": "k\"1"})
    check(out == {"ok": True, "n": 1}, "gateway_call parses json after banner")
    check(calls[0][-2] == json.dumps({"key": "k\"1"}) and calls[0][-1] == "--json" and "sessions.delete" in calls[0],
          "params passed as one argv element, unchanged")
    _p4_oc_fallback.subprocess.run = lambda argv, **kw: FakeProc(1, "", "boom")
    try:
        _p4_oc_fallback.gateway_call("x", {})
        check(False, "non-zero rc raises")
    except RuntimeError:
        check(True, "non-zero rc raises")
finally:
    _p4_oc_fallback.subprocess.run = real

# ---- 心跳信息：假 rpc ----
def fake_rpc(method, params, timeout=30.0):
    if method == "cron.list":
        return {"jobs": [{"id": "j1", "name": "heartbeat-main", "payload": {"kind": "heartbeat"}, "schedule": {"everyMs": 1800000}},
                         {"id": "j2", "name": "other", "payload": {"kind": "command"}}]}
    return {"entries": [{"status": "skipped", "error": "heartbeat skipped: no-route", "durationMs": 270},
                        {"status": "skipped", "error": "heartbeat skipped: no-route", "durationMs": 280}]}


hb = prompt_size.heartbeat_info(fake_rpc)
check(hb["every_min"] == 30 and hb["recent_all_skipped"] is True and hb["recent"] == 2, "heartbeat info from cron")
check(prompt_size.heartbeat_info(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))["error"] == "down", "heartbeat info survives rpc error")

sys.exit(1 if failed else 0)
