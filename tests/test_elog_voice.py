"""B1：守护进程接线的统一事件日志（tool.audit / tool.gate / pending.*、nudge.check / nudge.sent、voice.exit、turn.end）。
不建音频流、不加载模型：object.__new__ 拼守护进程 + 脚本化的假 TurnRunner，事件写进临时目录。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_elog_voice.py
"""
import json
import queue
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia import elog
from guanjia.audittail import PendingConfirm
from guanjia.events import EventHub
from guanjia.status import Health
from guanjia.turns import NUDGE_TEXT, Heard, TurnResult


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


class FakeTTS:
    def abort(self) -> None:
        pass


class ScriptRunner:
    def __init__(self, daemon, script) -> None:
        self.d = daemon
        self.script = list(script)  # [(reply, tools, aborted, error)]

    def run_reply(self, text, gen):
        reply, tools, aborted, error = self.script.pop(0)
        for _ in range(tools):
            self.d._on_tool({"ts": "t", "tool": "exec", "level": "L1", "decision": "allow", "state": "done", "summary": "x"})
        if aborted:
            self.d._gen += 1
        return TurnResult(reply=reply, spoken=[reply], aborted=aborted, error=error)


def make_daemon(script, log):
    d = object.__new__(voice.VoiceDaemon)
    d._lock, d._stop_evt = threading.Lock(), threading.Event()
    d._gen, d.state = 0, "LISTENING"
    d.play_q, d.text_q = queue.Queue(), queue.Queue()
    d._muted, d._paused = threading.Event(), threading.Event()
    d._play_buf, d._play_pos = np.zeros(0, dtype=np.float32), 0
    d._earcon = np.ones(4, dtype=np.float32)
    d._turn_first, d._turn_t0 = False, 0.0
    d.tray, d.last_error, d.sleep_state = None, "", "awake"
    d.tts, d.brain = FakeTTS(), SimpleNamespace(last_run_id=None)
    d.health = Health(True, True)
    d.runtime = SimpleNamespace(state="ACTIVE")
    d._exit_code = None
    d.hub = EventHub(elog=log)
    d.pending = PendingConfirm()
    d.status = lambda: {}
    d._refresh_tray = lambda: None
    d.runner = ScriptRunner(d, script)
    return d


def events(dirpath, *evs):
    out = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
    return [e for e in out if not evs or e["ev"] in evs]


voice.AUDIT_LAG_SEC = 0

with tempfile.TemporaryDirectory() as dirpath:
    log = elog.init("voice", log_dir=Path(dirpath), sync=True)
    try:
        # 1. 只说没做 -> nudge.check(true) + nudge.sent(parent/child)，追问轮有自己的 turn.end
        d = make_daemon([("我先查一下系统里的音频设备。", 0, False, None), ("做完了。", 1, False, None)], log)
        uid = d.hub.new_utterance("mic", text="打开音响驱动")
        d._handle_item(Heard("打开音响驱动", uid))
        chk, sent = events(dirpath, "nudge.check"), events(dirpath, "nudge.sent")
        check(len(chk) == 1 and chk[0]["uid"] == uid and chk[0]["data"]["decision"] is True and chk[0]["data"]["why"] == "nudge",
              "promise + 0 tools -> nudge.check decision=True under the parent uid")
        check(len(sent) == 1 and sent[0]["data"]["parent"] == uid and sent[0]["data"]["child"] == sent[0]["uid"] != uid
              and sent[0]["data"]["kind"] == "promise", "nudge.sent carries parent and child uid")
        ends = events(dirpath, "turn.end")
        check([e["uid"] for e in ends] == [uid, sent[0]["uid"]] and ends[1]["data"]["source"] == "nudge",
              "both the parent turn and the nudge turn get a turn.end")
        tools = events(dirpath, "tool.audit")
        check(len(tools) == 1 and tools[0]["uid"] == sent[0]["uid"] and tools[0]["data"]["tool_level"] == "L1"
              and tools[0]["data"]["tool"] == "exec", "tool.audit lands on the nudge turn, level name does not clash")

        # 2. 有工具 / 被打断 / 没承诺词
        d = make_daemon([("我先查一下，好了。", 1, False, None)], log)
        n0 = len(events(dirpath, "nudge.check"))
        d._handle_item(Heard("x", d.hub.new_utterance("mic", text="x")))
        last = events(dirpath, "nudge.check")[n0:]
        check(len(last) == 1 and last[0]["data"]["decision"] is False and last[0]["data"]["why"] == "has_tools", "has tools -> check False/has_tools")
        d = make_daemon([("我先查一下。", 0, False, "网关：boom")], log)
        n0 = len(events(dirpath, "nudge.check"))
        d._handle_item(Heard("x", d.hub.new_utterance("mic", text="x")))
        last = events(dirpath, "nudge.check")[n0:]
        check(len(last) == 1 and last[0]["data"]["decision"] is False and last[0]["data"]["why"] == "error", "error -> check False/error")
        d = make_daemon([("我先查一下。", 0, False, None)], log)
        d._muted.set()
        n0 = len(events(dirpath, "nudge.check"))
        d._handle_item(Heard("x", d.hub.new_utterance("mic", text="x")))
        check(events(dirpath, "nudge.check")[n0:][0]["data"]["why"] == "muted", "muted -> check False/muted")
        d = make_daemon([("我先查一下。", 0, True, None)], log)  # 被打断：这一轮压根不进追问判定，也就没有 check 事件
        n0 = len(events(dirpath, "nudge.check"))
        d._handle_item(Heard("x", d.hub.new_utterance("mic", text="x")))
        check(len(events(dirpath, "nudge.check")) == n0, "aborted turn never reaches the nudge decision")
        d = make_daemon([("已经打开了。", 0, False, None)], log)
        n0 = len(events(dirpath, "nudge.check"))
        d._handle_item(Heard("x", d.hub.new_utterance("mic", text="x")))
        check(len(events(dirpath, "nudge.check")) == n0, "plain reply is not logged as a nudge check")
        d = make_daemon([("这个操作需要审批，已经超时没批下来。", 0, False, None), ("做完了。", 1, False, None)], log)
        d._handle_item(Heard("x", d.hub.new_utterance("mic", text="x")))
        fk = [e for e in events(dirpath, "nudge.check") if e["data"]["kind"] == "fake-block"]
        check(len(fk) == 1 and fk[0]["data"]["decision"] is True, "fake-block claim -> check True")
        check(events(dirpath, "nudge.sent")[-1]["data"]["kind"] == "fake-block", "nudge.sent kind=fake-block")
        check(voice.NUDGE_TEXT == NUDGE_TEXT, "NUDGE_TEXT untouched")

        # 3. 拦截 / 待确认
        d = make_daemon([], log)
        d._on_tool({"ts": "t", "tool": "exec", "level": "L2", "decision": "block-need-confirm", "state": "blocked", "summary": "rm x"})
        d._on_gate({"ts": "t", "decision": "block-need-confirm", "summary": "删除 x"})
        d._on_gate({"ts": "t", "decision": "l2-confirmed", "summary": ""})
        got = events(dirpath, "tool.audit")[-1]
        check(got["level"] == "warn" and got["data"]["decision"] == "block-need-confirm", "blocked tool -> warn")
        check([e["ev"] for e in events(dirpath, "tool.gate", "pending.open", "pending.close")][-4:]
              == ["tool.gate", "pending.open", "tool.gate", "pending.close"], "gate events + pending open/close recorded")
        po = events(dirpath, "pending.open")[-1]
        check(po["level"] == "warn" and "删除" in po["msg"], "pending.open is a warn with the summary")

        # 4. 退出
        d = make_daemon([], log)
        d.request_exit(voice.EXIT_RESTART, "tray")
        d.request_exit(voice.EXIT_CRASH)
        ex = events(dirpath, "voice.exit")
        check(ex[0]["data"] == {"code": 3, "reason": "tray"} and ex[0]["level"] == "info", "restart exit -> info with reason")
        check(ex[1]["level"] == "warn" and ex[1]["data"]["code"] == 1, "unusual exit code -> warn")
    finally:
        log.close()
        elog._default = elog.NullLog()

print("ALL PASS test_elog_voice")
