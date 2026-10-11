"""权限 v2 补丁：待确认托盘提醒（audittail.PendingConfirm / status.tray_view / Tray.notify / voice._on_gate）
和「编造拦截」核对（turns 词表 / voice 追问接线）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_perm_v2b.py
"""
import json
import logging
import queue
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia.audittail import (GATE_DECISIONS, MAIN_SESSION_KEY, PENDING_CLOSE, PENDING_OPEN, PENDING_TTL_SEC, AuditTail,
                               PendingConfirm, gate_event, tool_entry)
from guanjia.status import COLORS, Health, tray_view
from guanjia.tray import Tray
from guanjia.turns import (BLOCK_CLAIM_WEAK_WORDS, BLOCK_CLAIM_WORDS, NUDGE_TEXT, NUDGE_TEXT_FAKE_BLOCK, SayItem, TurnResult,
                           claims_blocked, needs_fake_block_nudge)


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def line(decision: str, **kw) -> dict:
    return {"ts": "t", "kind": "tool", "sessionKey": MAIN_SESSION_KEY, "decision": decision, "summary": kw.pop("summary", "exec: x"), **kw}


# ================= audittail：gate_event / PendingConfirm =================
check(gate_event(line("block-need-confirm", tool="exec"))["decision"] == "block-need-confirm", "gate_event: block-need-confirm")
check(gate_event(line("l2-confirmed")) is not None and tool_entry(line("l2-confirmed")) is None,
      "gate_event sees l2-confirmed (no tool field) that tool_entry skips")
check(gate_event(line("allow", tool="exec")) is None, "gate_event ignores plain allow")
check(gate_event({**line("block-need-confirm"), "sessionKey": "agent:main:cron:x"}) is None, "gate_event only main session")
check(gate_event({**line("block-need-confirm"), "kind": "habit"}) is None, "gate_event only kind=tool")
check(gate_event("junk") is None, "gate_event tolerates junk")
check({"block-need-confirm"} <= PENDING_OPEN and {"l2-confirmed", "l2-dropped", "allow-confirmed"} <= PENDING_CLOSE, "decision sets")
check({"block-no-confirmer", "block-refused"} <= GATE_DECISIONS and not (PENDING_OPEN & PENDING_CLOSE), "gate set covers real blocks")

now = [1000.0]
p = PendingConfirm(clock=lambda: now[0])
check(p.current() == "" and p.feed(gate_event(line("l2-confirmed"))) == "", "nothing pending at start; close without open is a no-op")
check(p.feed(gate_event(line("block-need-confirm", summary="exec: del x"))) == "opened" and p.current() == "exec: del x", "block-need-confirm opens")
now[0] += PENDING_TTL_SEC - 1
check(p.current() == "exec: del x", "still pending just before the TTL")
now[0] += 2
check(p.current() == "", "expired after the 3 minute TTL")
check(p.feed(gate_event(line("block-need-confirm"))) == "opened", "re-open after expiry")
check(p.feed(gate_event(line("l2-confirmed"))) == "closed" and p.current() == "", "owner said allow -> closed")
check(p.feed(gate_event(line("allow-confirmed"))) == "", "retry-allowed after close changes nothing")
for d in ("l2-dropped", "l2-remembered", "allow-confirmed", "approval-timeout", "approval-deny"):
    p.feed(gate_event(line("block-need-confirm")))
    check(p.feed(gate_event(line(d))) == "closed" and p.current() == "", f"{d} closes")
check(p.feed(gate_event(line("block-no-confirmer"))) == "" and p.current() == "", "block-no-confirmer (cron) never becomes pending")
check(p.feed(gate_event(line("block-refused"))) == "" and p.current() == "", "block-refused never becomes pending")
check(p.feed(gate_event(line("approval-requested"))) == "opened", "legacy approval-requested also opens")
p.feed(gate_event(line("block-need-confirm", summary="B")))
check(p.current() == "B", "a newer pending replaces the older one")

# AuditTail.poll 同时回调 on_gate / on_tool
tmp = Path(tempfile.mkdtemp()) / "audit.jsonl"
tmp.write_text("", encoding="utf-8")
tools, gates = [], []
tail = AuditTail(tools.append, path=tmp, on_gate=gates.append)
tail.poll()
with tmp.open("a", encoding="utf-8") as f:
    f.write(json.dumps(line("block-need-confirm", tool="exec", level="L2")) + "\n")
    f.write(json.dumps(line("l2-confirmed")) + "\n")
    f.write(json.dumps(line("allow", tool="exec", level="L0")) + "\n")
tail.poll()
check([g["decision"] for g in gates] == ["block-need-confirm", "l2-confirmed"], "tail: gate callback for block and l2-confirmed only")
check([t["decision"] for t in tools] == ["block-need-confirm", "allow"], "tail: tool callback unchanged (l2-confirmed has no tool)")
tail2 = AuditTail(tools.append, path=tmp)  # 不传 on_gate 照旧工作
tail2.poll()
check(True, "tail without on_gate still works")

# ================= status：托盘待确认 =================
ok = Health(True, True)
v = tray_view(muted=False, gaming=False, busy=False, health=ok, pending="exec: del x")
check(v.key == "pending" and v.color == COLORS["pending"] and "等你确认" in v.label and "del x" in v.label, "pending -> purple view with the action")
check(tray_view(muted=False, gaming=True, busy=False, health=ok, pending="x").key == "pending", "pending beats gaming (color-only reminder in games)")
check(tray_view(muted=True, gaming=False, busy=True, health=ok, pending="x").key == "pending", "pending beats muted/busy")
check(tray_view(muted=False, gaming=False, busy=False, health=Health(False, True), pending="x").key == "error", "error beats pending")
check(tray_view(muted=False, gaming=False, busy=False, health=ok, pending="").key == "listening", "cleared -> back to listening")
check(len(tray_view(muted=False, gaming=False, busy=False, health=ok, pending="x" * 300).tooltip) <= 120, "tooltip still <= 120")
check(COLORS["pending"] not in [c for k, c in COLORS.items() if k != "pending"], "pending color is unique")

# ================= tray.notify =================
t = Tray(view=lambda: v, muted=lambda: False, on_toggle_mute=lambda: None, on_restart=lambda: None, on_exit=lambda: None)
check(t.notify("hi") is False, "notify without an icon is a no-op returning False")
sent = []
t._icon = SimpleNamespace(notify=lambda msg, title: sent.append((msg, title)))
check(t.notify("思绪在等你确认：x") is True and sent == [("思绪在等你确认：x", "思绪")], "notify forwards message with the default title (显示名 思绪)")
t.notify("y" * 400, "t" * 100)
check(len(sent[-1][0]) <= 250 and len(sent[-1][1]) <= 60, "notify truncates to the Windows balloon limits")
t._icon = SimpleNamespace(notify=lambda msg, title: (_ for _ in ()).throw(OSError("boom")))
check(t.notify("x") is False, "notify swallows backend errors")

# ================= turns：编造拦截词表 =================
check(all(BLOCK_CLAIM_WORDS) and all(BLOCK_CLAIM_WEAK_WORDS), "word lists are non-empty constants")
for reply in ("我的命令需要审批，已经超时很多次没批下来", "这条命令要审批，你确认一下放行", "被拦住了，需要你放行一下", "审批超时了，没批下来",
              "这个操作需要批准，我先等着", "刚才那条被拦了", "没批准，我换个办法", "需要确认，你说允许我就做。"):
    check(claims_blocked(reply), f"claims_blocked: {reply}")
for reply in ("已经打开了。", "现在三点。", "这个不需要审批，我直接做。", "没有被拦，已经好了", "要我打开音箱软件吗？", "我需要确认一下，你说的是哪个音箱？",
              "确认过了，打开了。", ""):
    check(not claims_blocked(reply), f"not a block claim: {reply!r}")
check(claims_blocked("这条命令需要审批吗？你说一声") is True, "strong word still counts inside a question")
check(needs_fake_block_nudge("需要审批", False), "claim + no gate record -> nudge")
check(not needs_fake_block_nudge("需要审批", True), "claim but a real block was recorded -> no nudge")
check(not needs_fake_block_nudge("需要审批", False, aborted=True), "aborted -> no nudge")
check(not needs_fake_block_nudge("需要审批", False, error="boom"), "error -> no nudge")
check(not needs_fake_block_nudge("好了", False), "no claim -> no nudge")
check("没有任何操作被拦截" in NUDGE_TEXT_FAKE_BLOCK and "直接调用工具" in NUDGE_TEXT_FAKE_BLOCK and "一句话" in NUDGE_TEXT_FAKE_BLOCK,
      "fake-block nudge text")
check(NUDGE_TEXT_FAKE_BLOCK != NUDGE_TEXT, "two distinct nudge texts")


# ================= voice 接线 =================
class FakeTTS:
    def abort(self) -> None:
        pass


class FakeTray:
    def __init__(self) -> None:
        self.notified: list[str] = []
        self.refreshed = 0

    def notify(self, message: str, title: str = "思绪") -> bool:
        self.notified.append(message)
        return True

    def refresh(self) -> None:
        self.refreshed += 1


class ScriptRunner:
    """按剧本回复：(reply, [本轮审计 decision...], aborted, error)；gate 类 decision 走 _on_gate，其余走 _on_tool。"""

    def __init__(self, daemon, script) -> None:
        self.d, self.script, self.texts = daemon, list(script), []

    def run_reply(self, text, gen):
        self.texts.append(text)
        reply, decisions, aborted, error = self.script.pop(0)
        for dec in decisions:
            ev = line(dec, tool="exec", level="L2")
            if gate_event(ev):
                self.d._on_gate(gate_event(ev))
            if tool_entry(ev):
                self.d._on_tool({"ts": "", "tool": "exec", "level": "L2", "decision": dec, "state": "done", "summary": "x"})
        if aborted:
            self.d._gen += 1
        return TurnResult(reply=reply, spoken=[reply], aborted=aborted, error=error)


def make_daemon(script):
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
    d.pending = PendingConfirm()
    d.runner = ScriptRunner(d, script)
    return d


voice.AUDIT_LAG_SEC = 0
FAKE = "这条命令要审批，你确认一下放行。"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record) -> None:
        self.lines.append(record.getMessage())


cap = Capture()
logging.getLogger("guanjia.voice").addHandler(cap)
logging.getLogger("guanjia.voice").setLevel(logging.INFO)

# 1. 声称要审批、本轮零拦截记录 -> 追发一次 NUDGE_TEXT_FAKE_BLOCK，日志 nudge sent: fake-block
d = make_daemon([(FAKE, [], False, None), ("已经打开了。", ["allow"], False, None)])
d._handle_item("打开音箱软件")
check(d.runner.texts == ["打开音箱软件", NUDGE_TEXT_FAKE_BLOCK], "fake-block claim -> nudged once with NUDGE_TEXT_FAKE_BLOCK")
check("nudge sent: fake-block" in cap.lines, "log line 'nudge sent: fake-block'")

# 2. 追发的那一轮又编 -> 不再追（每轮最多一次）；承诺词追发后再编也不追
d = make_daemon([(FAKE, [], False, None), (FAKE, [], False, None)])
d._handle_item("打开音箱软件")
check(len(d.runner.texts) == 2, "second fake claim in the nudge turn is not nudged again")
d = make_daemon([("我先查一下。", [], False, None), (FAKE, [], False, None)])
d._handle_item("打开音箱软件")
check(len(d.runner.texts) == 2 and d.runner.texts[1] == NUDGE_TEXT, "promise nudge and fake-block share the one-nudge limit")

# 3. 同一回复既有承诺词又声称被拦：只追一次，承诺词优先
d = make_daemon([("我先查一下，不过要审批。", [], False, None), ("好了。", ["allow"], False, None)])
d._handle_item("打开音箱软件")
check(d.runner.texts == ["打开音箱软件", NUDGE_TEXT], "promise + claim -> a single promise nudge")

# 4. 本轮真有拦截记录 -> 不追；有 l2-confirmed（没 tool 字段）也算有据
d = make_daemon([("需要确认，你说允许我就做。", ["block-need-confirm"], False, None)])
d._handle_item("删掉旧日志")
check(len(d.runner.texts) == 1, "real block-need-confirm in the turn -> no nudge")
d = make_daemon([("已放行，做完了。", ["l2-confirmed", "allow-confirmed"], False, None)])
d._handle_item("允许")
check(len(d.runner.texts) == 1, "confirm-flow records in the turn -> no nudge")
d = make_daemon([("被拦住了，没人能当场确认。", ["block-no-confirmer"], False, None)])
d._handle_item("x")
check(len(d.runner.texts) == 1, "block-no-confirmer counts as a real block")

# 5. 上一轮真拦下、这一轮没新记录（主人只嗯了一声）：待确认还在 -> 不追；了结之后再编才追
d = make_daemon([("还是需要确认，你说允许就行。", [], False, None)])
d.pending.feed(gate_event(line("block-need-confirm")))
d._handle_item("嗯")
check(len(d.runner.texts) == 1, "still-pending confirmation from the previous turn -> no nudge")
d = make_daemon([(FAKE, [], False, None), ("好了。", ["allow"], False, None)])
d.pending.feed(gate_event(line("block-need-confirm")))
d.pending.feed(gate_event(line("l2-dropped")))
d._handle_item("算了换个事")
check(len(d.runner.texts) == 2, "pending already closed -> fake claim is nudged")

# 6. 本轮调过别的工具（tool_search allow）也照样核对
d = make_daemon([(FAKE, ["allow", "allow"], False, None), ("好了。", ["allow"], False, None)])
d._handle_item("打开音箱软件")
check(len(d.runner.texts) == 2, "tool calls without any block record do not excuse the claim")

# 7. 上一轮的拦截记录不带到下一轮
d = make_daemon([("需要确认，你说允许。", ["block-need-confirm"], False, None), (FAKE, [], False, None), ("好了。", ["allow"], False, None)])
d._handle_item("第一句")
d.pending.feed(gate_event(line("l2-dropped")))
d._handle_item("第二句")
check(len(d.runner.texts) == 3, "turn gate set resets per turn")

# 8. 被打断 / 出错 / 静音 / 游戏中 / 主人已排新话 / say -> 不追
for script, setup, what in (((FAKE, [], True, None), None, "aborted"), ((FAKE, [], False, "网关：boom"), None, "error"),
                            ((FAKE, [], False, None), lambda x: x._muted.set(), "muted"),
                            ((FAKE, [], False, None), lambda x: x._paused.set(), "gaming"),
                            ((FAKE, [], False, None), lambda x: x.text_q.put("新的一句"), "owner queued")):
    d = make_daemon([script])
    if setup:
        setup(d)
    d._handle_item("打开音箱软件")
    check(len(d.runner.texts) == 1, f"{what} -> no fake-block nudge")
d = make_daemon([])
d.runner.run_say = lambda text, gen: TurnResult(reply=FAKE, spoken=[FAKE])
d._handle_item(SayItem(FAKE))
check(d.runner.texts == [], "say turn never nudges")

# 9. 待确认：托盘变色 + 气泡；游戏中只变色
d = make_daemon([])
d.tray = FakeTray()
check(d.view().key == "listening", "no pending -> normal view")
d._on_gate(gate_event(line("block-need-confirm", summary="exec: del D:\\old")))
check(d.view().key == "pending" and "del D:" in d.view().label, "pending -> tray view turns pending")
check(d.tray.notified == ["思绪在等你确认：exec: del D:\\old"] and d.tray.refreshed >= 1, "balloon text + tray refreshed")
d._on_gate(gate_event(line("l2-confirmed")))
check(d.view().key == "listening" and len(d.tray.notified) == 1, "confirmed -> view restored, no extra balloon")
d = make_daemon([])
d.tray = FakeTray()
d._paused.set()
d._on_gate(gate_event(line("block-need-confirm", summary="exec: rm x")))
check(d.view().key == "pending" and d.tray.notified == [] and d.tray.refreshed >= 1, "gaming: icon turns pending, no balloon")
d._paused.clear()
d._on_gate(gate_event(line("l2-dropped")))
check(d.view().key == "listening", "dropped -> restored")
d = make_daemon([])
d.tray = None
d._on_gate(gate_event(line("block-need-confirm")))
check(d.view().key == "pending", "no tray (--no-tray) still tracks pending without crashing")

# 10. 超时后恢复：不需要任何事件，view() 自己过期
now2 = [0.0]
d = make_daemon([])
d.pending = PendingConfirm(clock=lambda: now2[0])
d._on_gate(gate_event(line("block-need-confirm")))
check(d.view().key == "pending", "pending shown")
now2[0] += PENDING_TTL_SEC + 1
check(d.view().key == "listening", "view recovers after the TTL with no event (main loop refreshes on key change)")

print("ALL PASS test_perm_v2b")
