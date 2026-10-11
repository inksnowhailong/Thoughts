"""B3：网关可见性接线——Brain.stream 记流元信息、_on_turn_event 的 brain.run / brain.stream_end、
_fill_gateway_phases 读 trajectory 摘要成 gateway.run 事件 + blob（读不到重试、5 次都没有记 run_missing）。
不连网关、不碰线上库：httpx.MockTransport + 假的 run_summary。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p3_gateway_events.py
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

import guanjia.voice as voice
from guanjia import elog
from guanjia.brain import Brain
from guanjia.events import EventHub


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def events(dirpath, *evs):
    out = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
    return [e for e in out if not evs or e["ev"] in evs]


# ---------- 1. Brain.stream 记流里的元信息 ----------
SSE = (b'data: {"id":"chatcmpl_x1","model":"openclaw","choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n'
       b'data: {"id":"chatcmpl_x1","choices":[{"delta":{"content":"\xe5\xa5\xbd"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3}}\n\n'
       b"data: [DONE]\n\n")


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["messages"][-1]["content"] == "无元信息":
        return httpx.Response(200, content=b'data: {"id":"chatcmpl_x2","choices":[{"delta":{"content":"a"}}]}\n\ndata: [DONE]\n\n')
    return httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})


b = Brain(url="http://x/v1/chat/completions", token="t", transport=httpx.MockTransport(handler))
check(b.last_meta == {}, "no meta before the first stream")
check("".join(b.stream("你好")) == "你好" and b.last_run_id == "chatcmpl_x1", "stream text and run id unchanged")
first = b.last_meta
check(first == {"chunks": 2, "model": "openclaw", "finish_reason": "stop", "usage": {"prompt_tokens": 3}}, f"meta collected: {first}")
list(b.stream("无元信息"))
check(b.last_meta == {"chunks": 1} and first["chunks"] == 2, "each stream gets a fresh dict; keys the stream lacks are simply absent")

# ---------- 2. _on_turn_event：brain.run / brain.stream_end ----------
with tempfile.TemporaryDirectory() as dirpath:
    log = elog.init("voice", log_dir=Path(dirpath), sync=True)
    try:
        d = object.__new__(voice.VoiceDaemon)
        d.hub = EventHub(elog=log)
        d.brain = SimpleNamespace(last_run_id="chatcmpl_x1", last_meta={"chunks": 2, "model": "openclaw", "finish_reason": "stop"})
        uid = d.hub.new_utterance("mic", text="你好")
        d._turn_uid, d._turn_marks = uid, {"turn_start": 100.0}
        d._on_turn_event("first_token", {"t": 101.5})
        d._on_turn_event("stream_end", {"t": 103.0, "ok": True})
        d._on_turn_event("stream_end", {"t": 104.0, "ok": False, "error": "网关：boom"})
        run, ends = events(dirpath, "brain.run")[0], events(dirpath, "brain.stream_end")
        check(run["uid"] == uid and run["data"] == {"run_id": "chatcmpl_x1", "first_ms": 1500}, "brain.run ties uid to the gateway run id")
        check(ends[0]["level"] == "info" and ends[0]["data"]["ok"] is True and ends[0]["data"]["chunks"] == 2
              and ends[0]["data"]["finish_reason"] == "stop" and ends[0]["data"]["ms"] == 3000, "brain.stream_end carries stream meta")
        check(ends[1]["level"] == "error" and ends[1]["data"]["error"] == "网关：boom", "broken stream -> error level with the reason")

        # ---------- 3. _fill_gateway_phases ----------
        summary = {"run_id": "chatcmpl_x1", "provider": "p", "model": "p/DeepSeek-V4-Flash", "channel": "webchat", "stop": "stop",
                   "final_status": "success", "usage": {"input": 10, "output": 5}, "aborted": False, "timed_out": False,
                   "prompt_error": None, "texts": ["我先查一下"], "tools": [{"name": "exec", "ok": False, "err": "boom"}],
                   "phases": {"session_started": 101.0, "prompt_submitted": 102.0},
                   "ctx": {"prompt": "[语音] 你好 Bearer abcdefghijklmnop", "messages_total": 3}}
        calls = []

        def fake_summary(run_id):
            calls.append(run_id)
            return None if len(calls) == 1 else dict(summary)

        sleeps = []
        real_sleep = voice.time.sleep
        voice.run_summary = fake_summary
        voice.time.sleep = lambda s: sleeps.append(s)
        try:
            d._fill_gateway_phases(uid, "chatcmpl_x1", {"turn_start": 100.0, "first_token": 104.0})
            check(len(calls) == 2 and sleeps == [1.0], "first read misses, second succeeds after one 1s wait")
            gw = events(dirpath, "gateway.run")
            check(len(gw) == 1 and gw[0]["uid"] == uid and gw[0]["level"] == "warn", "gateway.run emitted, warn because a tool failed")
            g = gw[0]["data"]
            check(g["model"] == "p/DeepSeek-V4-Flash" and g["stop"] == "stop" and g["usage"]["input"] == 10
                  and g["tools"][0]["ok"] is False and g["texts"] == ["我先查一下"] and g["phases"]["session_started"] == 101.0,
                  "gateway.run has model / stop / usage / tools / texts / phases")
            check("工具 1 次（失败 1）" in gw[0]["msg"] and "stop=stop" in gw[0]["msg"], "human-readable msg")
            blob = (Path(dirpath) / "blobs" / g["blob"]).read_text(encoding="utf-8")
            check("messages_total" in blob and "abcdefghijklmnop" not in blob, "context goes to a redacted blob, event only keeps the path")
            rec = d.hub.record(uid)
            check(rec["run_id"] == "chatcmpl_x1" and rec["timings"]["gateway_prep_ms"] == 2000 and rec["timings"]["model_first_ms"] == 2000,
                  "run_id and timing split still written to the panel record")

            # 读一直拿不到：最多 5 次，记 run_missing；不抛异常
            calls.clear()
            sleeps.clear()
            voice.run_summary = lambda run_id: calls.append(run_id)
            d._fill_gateway_phases(uid, "chatcmpl_none", {"turn_start": 100.0})
            miss = events(dirpath, "gateway.run_missing")
            check(len(calls) == 5 and len(miss) == 1 and miss[0]["level"] == "warn" and miss[0]["data"]["run_id"] == "chatcmpl_none",
                  "5 tries then gateway.run_missing")

            # 读网关库抛异常：吞掉，不影响语音
            def boom(run_id):
                raise RuntimeError("db locked")

            voice.run_summary = boom
            d._fill_gateway_phases(uid, "chatcmpl_err", {"turn_start": 100.0})
            check(len(events(dirpath, "gateway.run_missing")) == 2, "exceptions from the gateway db are swallowed")

            # 一切正常的 run：info 级别
            ok = dict(summary, tools=[{"name": "exec", "ok": True}])
            voice.run_summary = lambda run_id: dict(ok)
            d._fill_gateway_phases(uid, "chatcmpl_ok", {"turn_start": 100.0})
            check(events(dirpath, "gateway.run")[-1]["level"] == "info", "healthy run -> info")
        finally:
            voice.time.sleep = real_sleep
    finally:
        log.close()
        elog._default = elog.NullLog()

# ---------- 4. 在后台线程里不拖慢：_handle_item 只起线程，不同步读库（结构检查） ----------
src = Path(voice.__file__).read_text(encoding="utf-8")
check("threading.Thread(target=self._fill_gateway_phases" in src, "gateway summary runs in a background thread after the turn")

print("ALL PASS test_p3_gateway_events")
