"""P2.1 调试面板数据源用例：事件总线与每句话记录、日志归类与脱敏、审计跟随、网关会话库查时间点、
TurnRunner 进度事件、网关 runId。不碰音频；网络只用 httpx 假传输。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p21_events.py
"""
import json
import logging
import sqlite3
import tempfile
from pathlib import Path

import httpx
import numpy as np

from guanjia.audittail import AuditTail, tool_entry
from guanjia.brain import Brain
from guanjia.events import STAGES, EventHub, HubLogHandler, NullHub, classify_log, redact
from guanjia.runtrace import run_phases
from guanjia.turns import TurnRunner


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


tmpdir = Path(tempfile.mkdtemp(prefix="gj_p21e_"))

# ---------- 1. 事件总线与记录 ----------
clock = [1_790_000_000.0]
hub = EventHub(keep_records=3, records_file=tmpdir / "utt.jsonl", clock=lambda: clock[0])
q = hub.subscribe()
uid = hub.new_utterance("mic", text="管家打开记事本", score=0.6)
hub.stage(uid, "asr", "done", "管家打开记事本")
clock[0] += 0.5
hub.stage(uid, "wake", "done", "唤醒词「管家」")
hub.update(uid, timings={"asr_ms": 120})
hub.update(uid, timings={"queue_ms": 5})
hub.add_tool(uid, {"tool": "exec", "level": "L1", "decision": "allow"})
hub.finish(uid, verdict="accept", reply="打开了。")
rec = hub.record(uid)
check(STAGES[0] == "mic" and STAGES[-1] == "barge" and len(STAGES) == 10, "ten stages from mic to barge")
check(rec["stages"]["wake"]["state"] == "done" and rec["stages"]["asr"]["note"] == "管家打开记事本", "stage states kept per record")
check(rec["timings"] == {"asr_ms": 120, "queue_ms": 5}, "timings merged across updates")
check(rec["tools"][0]["tool"] == "exec" and rec["done"] and rec["verdict"] == "accept", "tools and final fields")
check([t["stage"] for t in rec["timeline"]] == ["asr", "wake"], "timeline in order")
saved = [json.loads(x) for x in (tmpdir / "utt.jsonl").read_text(encoding="utf-8").splitlines()]
check(len(saved) == 1 and saved[0]["id"] == uid, "finished record appended to jsonl")
kinds = []
while not q.empty():
    kinds.append(q.get_nowait()["type"])
check(kinds.count("stage") == 2 and "record" in kinds, "subscriber receives stage and record events")
hub.stage(None, "vad", "active", "有人在说话")
check(hub.live["vad"]["state"] == "active", "live stage without utterance id")
for i in range(4):
    hub.new_utterance("mic", text=f"第{i}句")
check(len(hub.records()) == 3 and hub.record(uid) is None, "only the newest N records kept in memory")
seq = hub.last_seq
hub.stage(None, "asr", "done")
check([e["stage"] for e in hub.events_since(seq)] == ["asr"], "events_since replays after a sequence number")
hub.unsubscribe(q)
check(NullHub().new_utterance("mic") is None and NullHub().stage("x", "asr", "done") is None, "NullHub swallows calls")

# ---------- 2. 日志归类与脱敏 ----------
check(classify_log("httpx", 'HTTP Request: GET http://127.0.0.1:18791/health "HTTP/1.0 200 OK"') == "health", "health check log")
check(classify_log("httpx", "HTTP Request: POST http://127.0.0.1:18791/tts") == "tts", "tts request log")
check(classify_log("httpx", "HTTP Request: POST http://127.0.0.1:18789/v1/chat/completions") == "brain", "gateway log")
check(classify_log("guanjia.voice", "barge-mon rms_peak=0.1") == "barge-mon", "barge-mon log")
check(classify_log("guanjia.voice", "拒绝：没有唤醒词 text='x'") == "gate", "gate log")
check(classify_log("guanjia.voice", "[我 0.60] 管家开灯") == "asr", "recognized text log")
check(classify_log("guanjia.voice", "tool exec L1 allow: x") == "tools", "tool log")
check("abc123def456" not in redact("Authorization: Bearer abc123def456ghi"), "bearer token masked")
check("sk-***" in redact("key sk-ABCDEFGH12345678"), "sk- key masked")
check("s3cretvalue1" not in redact('"token": "s3cretvalue1"'), "token field masked")
h2 = EventHub()
lg = logging.getLogger("guanjia.voice.testlog")
lg.addHandler(HubLogHandler(h2))
lg.setLevel(logging.INFO)
lg.info("拒绝：没有唤醒词 text=%r", "开灯")
check(h2.logs()[-1]["module"] == "gate" and "没有唤醒词" in h2.logs()[-1]["msg"], "log handler pushes classified lines")

# ---------- 3. 审计跟随 ----------
audit = tmpdir / "audit.jsonl"
audit.write_text('{"kind":"tool","tool":"read","sessionKey":"agent:main:main","decision":"allow"}\n', encoding="utf-8")
got = []
tail = AuditTail(got.append, path=audit)
check(tail.poll() == 0 and got == [], "first poll only seeks to the end")
with audit.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"ts": "t1", "kind": "tool", "tool": "exec", "level": "L3", "decision": "approval-requested",
                        "sessionKey": "agent:main:main", "summary": "exec: launch lol"}) + "\n")
    f.write(json.dumps({"kind": "tool", "tool": "exec", "decision": "allow", "sessionKey": "agent:main:openclaw-weixin:x"}) + "\n")
    f.write(json.dumps({"kind": "tool", "tool": "exec", "decision": "allow-inner-dedup", "sessionKey": "agent:main:main"}) + "\n")
    f.write('{"kind":"tool","tool":"web_fe')
check(tail.poll() == 1 and got[0]["state"] == "active" and got[0]["level"] == "L3", "main-session tool picked up, others skipped")
with audit.open("a", encoding="utf-8") as f:
    f.write('tch","level":"L0","decision":"allow","sessionKey":"agent:main:main"}\n')
check(tail.poll() == 1 and got[1]["tool"] == "web_fetch" and got[1]["state"] == "done", "half-written line completed next poll")
check(tool_entry({"kind": "tool", "tool": "exec", "decision": "approval-timeout", "sessionKey": "agent:main:main"})["state"]
      == "blocked", "approval timeout shown as blocked")

# ---------- 4. 网关会话库 ----------
db = tmpdir / "agent.sqlite"
con = sqlite3.connect(db)
con.execute("pragma journal_mode=wal")
con.execute("create table trajectory_runtime_events (session_id text, seq integer, run_id text, event_json text, created_at integer)")
rows = [(1, "session.started", 1000), (2, "prompt.submitted", 3500), (3, "tool.call", 5000), (4, "model.completed", 8000)]
con.executemany("insert into trajectory_runtime_events values ('s', ?, 'chatcmpl_x', ?, ?)",
                [(s, json.dumps({"type": t}), 1_790_000_000_000 + ms) for s, t, ms in rows])
con.commit()
ph = run_phases("chatcmpl_x", db)
check(ph and abs(ph["prompt_submitted"] - (1_790_000_000 + 3.5)) < 1e-6 and "first_tool" in ph, "phases read from WAL db read-only")
con.close()
check(run_phases("nope", db) is None and run_phases("x", tmpdir / "missing.sqlite") is None, "unknown run / missing db -> None")

# ---------- 5. TurnRunner 进度事件 ----------
evs = []
r = TurnRunner(lambda t: iter(["好的，", "马上打开记事本。"]), lambda s: (np.zeros(10, dtype=np.float32), 16000),
               lambda g, p, sr: None, lambda: 0, first_soft_min=3, on_event=lambda n, d: evs.append(n))
res = r.run_reply("x", 0)
check(res.spoken == ["好的，", "马上打开记事本。"], "first_soft_min=3 cuts the first comma early")
check(evs[0] == "first_token" and evs[-1] == "stream_end" and evs.count("tts_done") == 2 and evs.count("enqueued") == 2,
      "progress events emitted in order")
r2 = TurnRunner(lambda t: iter(["好。"]), lambda s: (np.zeros(1), 16000), lambda g, p, sr: None, lambda: 0,
                on_event=lambda n, d: 1 / 0)
check(r2.run_reply("x", 0).spoken == ["好。"], "a failing observer never breaks the turn")

# ---------- 6. 网关 runId ----------
def handler(request):
    body = 'data: {"id":"chatcmpl_abc","choices":[{"delta":{"content":"嗯"}}]}\n\ndata: [DONE]\n\n'
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8"))


b = Brain(token="t", session_key="agent:main:main", model="", transport=httpx.MockTransport(handler))
check(list(b.stream("x")) == ["嗯"] and b.last_run_id == "chatcmpl_abc", "brain remembers the gateway run id")

print("ALL PASS test_p21_events")
