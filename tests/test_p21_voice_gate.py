"""P2.1 守护进程接线用例：不建音频流、不加载模型，用 object.__new__ 拼一个守护进程（真的事件总线和内存设置），验证
唤醒词/碎音/声纹门的去留、只喊唤醒词的应答和免喊窗口、注入接口、面板控制、设置热生效、一轮结束的记录与耗时、
工具调用挂到当前轮，以及托盘菜单和固定到任务栏。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p21_voice_gate.py
"""
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia.barge import BargeConfig, BargeDetector
from guanjia.events import EventHub
from guanjia.settings import SettingsStore
from guanjia.status import Health
from guanjia.tray import Tray, promote_tray_icon
from guanjia.turns import Heard, SayItem, TurnResult
from guanjia.wake import WakeMatcher


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


class FakeRunner:
    def __init__(self):
        self.got = []

    def run_reply(self, text, gen):
        self.got.append(text)
        d.runner_events("first_token")
        return TurnResult(reply="好的，打开了。", spoken=["好的，打开了。"])

    def run_say(self, text, gen):
        return TurnResult(reply=text, spoken=[text])


def make():
    d = object.__new__(voice.VoiceDaemon)
    d._lock, d._stop_evt = threading.Lock(), threading.Event()
    d._gen, d.state, d.sr = 0, "LISTENING", 16000
    d.play_q, d.text_q = queue.Queue(), queue.Queue()
    d._muted, d._paused = threading.Event(), threading.Event()
    d._play_buf, d._play_pos = np.zeros(0, dtype=np.float32), 0
    d._earcon = np.ones(4, dtype=np.float32)
    d._turn_first, d._turn_t0 = False, 0.0
    d.tray, d.last_error, d.sleep_state, d.gate_mode = None, "", "awake", "enforce"
    d.tts = SimpleNamespace(abort=lambda: None)
    d.brain = SimpleNamespace(last_run_id=None)
    d.health = Health(True, True)
    d.runtime = SimpleNamespace(state="ACTIVE")
    d._exit_code = None
    d.hub = EventHub()
    d.store = SettingsStore(None)
    d.wake = WakeMatcher(("管家",))
    d.gate = SimpleNamespace(threshold=0.45)
    d.barge = BargeDetector(BargeConfig())
    d._ack_text = ""
    d._followup_until = 0.0
    d.runner = FakeRunner()
    d.runner_events = lambda name: d._on_turn_event(name, {"t": time.time()})
    return d


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


d = make()

# ---------- 1. 去留 ----------
r = d._on_heard("玩还绿吧", 0.6, source="mic", stream_text="")
check(r["verdict"] == "rejected" and r["reason"] == "没有唤醒词" and d.text_q.empty() and d.play_q.empty(),
      "no wake word: nothing queued, no earcon")
rec = d.hub.record(r["uid"])
check(rec["done"] and rec["stages"]["wake"]["state"] == "blocked" and rec["stages"]["speaker"]["state"] == "done"
      and rec["stages"]["speaker"]["note"] == "0.60",
      "outside window a high score is noted only; wake blocked")
r = d._on_heard("唉", 0.41, source="mic")
check(r["verdict"] == "ignored" and d.hub.record(r["uid"])["stages"]["asr"]["state"] == "blocked", "grunt ignored at recognition box")
d._followup_until = time.monotonic() + 30
r = d._on_heard("开灯", 0.28, source="mic")
rec = d.hub.record(r["uid"])
check(r["verdict"] == "rejected" and rec["stages"]["speaker"]["state"] == "blocked"
      and rec["stages"]["speaker"]["note"] == "声纹 0.28 < 0.45",
      "in window, no wake word: low voiceprint blocked with reason")
check(rec["stages"]["wake"]["state"] == "done" and rec["stages"]["wake"]["note"] == "免喊窗口内",
      "voiceprint reject inside the window still shows wake as done")
d._followup_until = 0.0
r = d._on_heard("管家打开记事本", 0.28, source="mic")
rec = d.hub.record(r["uid"])
check(r["verdict"] == "accept" and rec["stages"]["speaker"]["state"] == "done"
      and rec["stages"]["speaker"]["note"] == "0.28（唤醒词放行，不作门槛）",
      "wake word + low score accepted; speaker note says the gate was skipped")
drain(d.text_q)
drain(d.play_q)
r = d._on_heard("关家，打开记事本", 0.6, source="mic", seg_sec=1.2, asr_ms=150, spk_ms=40)
items = drain(d.text_q)
check(r["verdict"] == "accept" and len(items) == 1 and isinstance(items[0], Heard) and items[0].text == "打开记事本"
      and items[0].uid == r["uid"], "wake word accepted, stripped text queued with record id")
check(len(drain(d.play_q)) == 1, "earcon played on accept")
check(d.hub.record(r["uid"])["timings"] == {"asr_ms": 150, "speaker_ms": 40}, "recognition timings recorded")
accepted_uid = r["uid"]

# ---------- 2. 只喊唤醒词 → 应答 + 免喊窗口 ----------
r = d._on_heard("管家", 0.6, source="mic")
check(r["verdict"] == "wake_only" and d.text_q.empty(), "wake word alone is not sent to the gateway")
check(len(drain(d.play_q)) == 1 and d._followup_until > time.monotonic() + 19, "ack played and 20s window opened")
r = d._on_heard("打开记事本", 0.6, source="mic")
check(r["verdict"] == "accept" and r["in_window"] and drain(d.text_q)[0].text == "打开记事本", "window: no wake word needed")
drain(d.play_q)
d._followup_until = 0.0
check(d._on_heard("打开记事本", 0.6, source="mic")["verdict"] == "rejected", "window expired: wake word needed again")

# ---------- 3. 注入 ----------
r = d.inject("打开记事本", dry_run=False)
check(r["verdict"] == "rejected" and d.text_q.empty(), "inject without wake word never reaches the gateway")
r = d.inject("管家，打开记事本", dry_run=True)
check(r["verdict"] == "accept" and r["dry_run"] and d.text_q.empty() and d.play_q.empty(), "dry run decides but does nothing")
check("模拟" in d.hub.record(r["uid"])["reason"], "dry run marked in the record")

# ---------- 4. 一轮结束：记录、耗时、免喊窗口 ----------
d._handle_item(Heard("打开记事本", accepted_uid))
rec = d.hub.record(accepted_uid)
check(d.runner.got == ["打开记事本"] and rec["done"] and rec["reply"] == "好的，打开了。", "turn recorded on the same id")
check(rec["stages"]["play"]["state"] == "done" and "gateway_first_ms" in rec["timings"], "play done, gateway timing recorded")
check(d._followup_until > time.monotonic() + 19 and d.state == "LISTENING", "window opens after the last sentence")
d._followup_until = 0.0
d._handle_item(SayItem("念这句", None))
check(d.state == "LISTENING" and d._followup_until == 0.0, "say item handled without opening the window")

# ---------- 5. 工具调用挂到当前轮 ----------
uid = d.hub.new_utterance("mic", text="管家打开英雄联盟")
d._turn_uid = uid
d._on_tool({"tool": "exec", "level": "L3", "decision": "approval-requested", "state": "active", "summary": "launch lol"})
rec = d.hub.record(uid)
check(rec["tools"][0]["level"] == "L3" and rec["stages"]["tools"]["note"] == "exec L3 等审批", "tool attached, approval shown")
d._turn_uid = None
d._on_tool({"tool": "read", "level": "L0", "decision": "allow", "state": "done", "summary": "x"})
check(len(d.hub.record(uid)["tools"]) == 1, "tool outside a voice turn is only logged")

# ---------- 6. 面板控制与设置 ----------
check(d._panel_control("mute") == (True, "已静音") and d._muted.is_set(), "panel mute")
check(d._panel_control("mute")[0] and d._muted.is_set(), "mute twice stays muted")
check(d._panel_control("unmute")[0] and not d._muted.is_set(), "panel unmute")
ok, msg = d._panel_control("quit")
time.sleep(0.8)
check(ok and d._exit_code == voice.EXIT_QUIT and d._stop_evt.is_set(), "panel quit -> exit code 0 (supervisor stops)")
d._stop_evt.clear()
d._exit_code = None
d._panel_control("restart")
time.sleep(0.8)
check(d._exit_code == voice.EXIT_RESTART, "panel restart -> exit code 3")
d.store.subscribe(d._apply_settings)
d.store.update({"wake_words": ["小管"], "spk_threshold": 0.5, "barge_rms_min": 0.006})
check(d.wake.words == ("小管",) and d.gate.threshold == 0.5 and d.barge.cfg.rms_min == 0.006, "settings applied hot")
d._followup_until = 0.0
check(d._on_heard("管家开灯", 0.6, source="mic")["verdict"] == "rejected", "old wake word no longer works")
check(d._on_heard("小管开灯", 0.6, source="mic")["verdict"] == "accept", "new wake word works immediately")
s = d.status()
check(s["wake_words"] == ["小管"] and "followup_left" in s and s["panel"].endswith("/panel"), "status shows wake words and panel url")

# ---------- 7. 托盘 ----------
class FakePystray:
    class Menu:
        SEPARATOR = "---"

    class MenuItem:
        def __init__(self, text, action, **kw):
            self.text = text


t = Tray(view=d.view, muted=lambda: False, on_toggle_mute=lambda: None, on_restart=lambda: None, on_exit=lambda: None,
         on_open_panel=lambda: None)
labels = [i.text for i in t.menu_items(FakePystray) if i != "---" and isinstance(i.text, str)]
check(labels[0].startswith("打开面板") and "退出语音" in labels and "重启语音" in labels, "tray menu has open panel")


class FakeReg:
    def __init__(self):
        self.vals = {"a": {"InitialTooltip": "管家：在听", "ExecutablePath": "x\\python.exe"},
                     "b": {"InitialTooltip": "管家：启动中", "IsPromoted": 0},
                     "c": {"InitialTooltip": "Steam"}}
        self.promoted = []

    def entries(self):
        return list(self.vals.items())

    def promote(self, name):
        self.promoted.append(name)


reg = FakeReg()
check(promote_tray_icon(reg) == ["a"] and reg.promoted == ["a"], "only our never-touched tray entry is promoted")

print("ALL PASS test_p21_voice_gate")
