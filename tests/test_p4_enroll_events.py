"""B4：声纹注册逐段事件 + 依赖状态变化事件。
EnrollSession.feed 的每个返回都带 info / group / total（拒收和「正在出声」也有），守护进程的 _enroll_take 每段记一条 enroll.seg，
面板动作记 enroll.start / state / discard / abort / save，_on_health 只在变化时记 dep.change。
不建音频流、不加载模型：假嵌入 + object.__new__ 拼守护进程。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p4_enroll_events.py
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
from guanjia.enroll_session import MIN_TOTAL, EnrollSession
from guanjia.events import EventHub
from guanjia.settings import SettingsStore
from guanjia.status import Health


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


rng = np.random.default_rng(3)
BASE = rng.standard_normal(192)


def unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def embed(samples):
    return unit(BASE + 0.1 * rng.standard_normal(192))


loud = (0.3 * np.sin(np.arange(16000 * 3) / 7)).astype(np.float32)
silent = np.zeros(16000 * 3, dtype=np.float32)  # 全零：被音箱噪声门抹光
short = loud[:8000]  # 0.5 秒
quiet = (0.01 * np.sin(np.arange(16000 * 3) / 7)).astype(np.float32)  # 峰值 0.01 < 0.03

# ---------- 1. EnrollSession.feed 的返回都带 info / group / total ----------
s = EnrollSession("我", embed, old_refs=None)
for name, seg in (("silent", silent), ("short", short), ("quiet", quiet)):
    r = s.feed(seg)
    check(r["ok"] is False and "zero_ratio" in r["info"] and r["group"] == "near" and r["total"] == 0, f"{name} rejected with info/group/total")
check(s.feed(silent)["info"]["zero_ratio"] == 1.0 and s.feed(silent)["info"]["effective_sec"] == 0.0, "silent segment: zero_ratio 1.0, effective 0")
r_play = s.feed(loud, playing=True)
check(r_play["ok"] is False and "出声" in r_play["reason"] and r_play["info"]["effective_sec"] > 2 and r_play["group"] == "near",
      "playing is still refused first (reason unchanged) but now carries info")
r_silent_play = s.feed(silent, playing=True)
check("出声" in r_silent_play["reason"], "playing outranks a bad-quality segment (judging order unchanged)")
r = s.feed(loud)
check(r["ok"] and r["group"] == "near" and r["total"] == 1 and r["info"]["peak"] > 0.2, "accepted segment: group and total after adding")
check(s.state == "recording" and len(s.samples) == 1, "playing/rejected segments did not add samples")
while s.state == "recording":
    s.feed(loud)
check(s.state == "testing" and len(s.samples) == 11, "recording fills 11 segments then moves to testing")
r = s.feed(loud)
check(r["ok"] and r["group"] == "test" and r["total"] == 11 and r["test_score"] is not None and s.state == "ready", "self-test segment reports group=test and its score")
r = s.feed(loud)
check(r == {"ok": False, "reason": "not_active", "group": None, "total": 11}, "after ready, feed says not_active")

# ---------- 2. 守护进程接线 ----------


class FakeSess:
    """feed 返回预设的假会话。"""

    def __init__(self, results):
        self.results = list(results)
        self.state = "recording"
        self.samples = []
        self.last = None

    def feed(self, samples, *, playing=False):
        self.last = self.results.pop(0)
        if self.last.get("_to"):
            self.state = self.last["_to"]
        return {k: v for k, v in self.last.items() if k != "_to"}

    def expired(self):
        return False


def events(dirpath, *evs):
    out = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
    return [e for e in out if not evs or e["ev"] in evs]


def make(log):
    d = object.__new__(voice.VoiceDaemon)
    d._lock, d._stop_evt = threading.Lock(), threading.Event()
    d._enroll_lock = threading.Lock()
    d.state = "LISTENING"
    d.text_q = queue.Queue()
    d._muted, d._paused = threading.Event(), threading.Event()
    d.hub = EventHub(elog=log)
    d.store = SettingsStore(None)
    d.speaker_gate, d.enroll, d._spare_gate = True, None, None
    d._play_buf, d._play_pos, d.play_q = np.zeros(0, dtype=np.float32), 0, queue.Queue()
    d._last_play_t = -1.0
    return d


class FakeGate:
    def __init__(self):
        self.people = {"我": np.stack([unit(rng.standard_normal(192))])}
        self.threshold = 0.0
        self.legacy = False
        self.load_error = None
        self.saved = []

    def embed(self, samples):
        return embed(samples)

    def refs(self, name):
        return self.people.get(name)

    def names(self):
        return list(self.people)

    def counts(self):
        return {k: len(v) for k, v in self.people.items()}

    def save_person(self, name, embs):
        self.saved.append(name)
        self.people[name] = embs
        return SimpleNamespace(name="speakers.npz.bak")


with tempfile.TemporaryDirectory() as dirpath:
    log = elog.init("voice", log_dir=Path(dirpath), sync=True)
    try:
        d = make(log)
        d.enroll = FakeSess([
            {"ok": False, "reason": "声音太小，被音箱截掉了一大半，大声一点或近一点", "group": "near", "total": 2,
             "info": {"sec": 3.0, "effective_sec": 0.4, "zero_ratio": 0.87, "peak": 0.021}},
            {"ok": False, "reason": "电脑正在出声，这段不算", "group": "near", "total": 2,
             "info": {"sec": 3.0, "effective_sec": 2.9, "zero_ratio": 0.03, "peak": 0.2}},
            {"ok": False, "reason": "这段不像同一个人（0.21），没收，请再说一遍", "group": "far", "total": 5, "self_score": 0.21,
             "info": {"sec": 3.0, "effective_sec": 2.5, "zero_ratio": 0.2, "peak": 0.1}},
            {"ok": True, "group": "loud", "total": 11, "_to": "testing", "info": {"sec": 3.0, "effective_sec": 2.8, "zero_ratio": 0.1, "peak": 0.4}},
        ])
        d._last_play_t = 5.0
        d._enroll_take(SimpleNamespace(samples=loud, start=5.2))   # 和播放重叠
        d._enroll_take(SimpleNamespace(samples=loud, start=9.0))
        d._enroll_take(SimpleNamespace(samples=loud, start=9.5))
        d._enroll_take(SimpleNamespace(samples=loud, start=10.0))
        segs = events(dirpath, "enroll.seg")
        check(len(segs) == 4, "one enroll.seg per segment")
        a = segs[0]
        check(a["level"] == "warn" and a["data"]["ok"] is False and a["data"]["overlap"] is True and a["data"]["play_gap"] == 0.2,
              "overlap segment: warn, overlap=True, play_gap")
        check({"effective_sec", "zero_ratio", "peak", "sec", "group", "total", "overlap", "playing"} <= set(a["data"]), f"seg data has the diagnostics: {sorted(a['data'])}")
        check(a["data"]["effective_sec"] == 0.4 and a["data"]["zero_ratio"] == 0.87 and a["data"]["peak"] == 0.021, "diagnostic numbers pass through")
        check(segs[2]["data"]["self_score"] == 0.21 and segs[2]["data"]["group"] == "far", "self_score and group recorded for 'not the same person'")
        check(segs[3]["level"] == "info" and segs[3]["msg"] == "收下" and segs[3]["data"]["ok"] is True, "accepted segment is info")
        st = events(dirpath, "enroll.state")
        check(len(st) == 1 and st[0]["data"]["old"] == "recording" and st[0]["data"]["new"] == "testing", "state change recorded exactly once")
        check(d.hub.live["speaker"]["state"] == "done", "panel live status still updated")

        # 面板动作：start / discard / enough / cancel / save
        g = FakeGate()
        d = make(log)
        d.gate = g
        d._speaker_action({"action": "start", "name": "我"})
        start = events(dirpath, "enroll.start")[-1]
        check(start["data"]["name"] == "我" and start["data"]["old_refs"] == 1 and start["data"]["min_effective_sec"] == 1.5
              and start["data"]["min_total"] == MIN_TOTAL and "max_zero_ratio" in start["data"], "enroll.start records the thresholds in force")
        for _ in range(MIN_TOTAL):
            d.enroll.feed(loud)
        d._speaker_action({"action": "discard_last"})
        check(events(dirpath, "enroll.discard")[-1]["data"]["total"] == MIN_TOTAL - 1, "discard recorded with the new total")
        d.enroll.feed(loud)
        d._speaker_action({"action": "enough"})
        check(events(dirpath, "enroll.state")[-1]["data"]["new"] == "testing", "'enough' recorded as a state change")
        d.enroll.feed(loud)
        d._speaker_action({"action": "commit"})
        sv = events(dirpath, "enroll.save")[-1]
        check(sv["data"]["n"] == MIN_TOTAL and sv["data"]["test_score"] is not None and sv["data"]["backup"] == "speakers.npz.bak", "enroll.save has n / scores / backup")
        d._speaker_action({"action": "start", "name": "我"})
        d.enroll.feed(loud)
        d._speaker_action({"action": "cancel"})
        ab = events(dirpath, "enroll.abort")[-1]
        check(ab["level"] == "warn" and ab["data"]["reason"] == "cancelled" and ab["data"]["total"] == 1, "cancel -> enroll.abort with reason and segments so far")

        # 超时
        d._speaker_action({"action": "start"})
        clock = [0.0]
        d.enroll._now, d.enroll.started = (lambda: clock[0]), 0.0
        clock[0] = 10_000.0
        d._active_enroll()
        check(events(dirpath, "enroll.abort")[-1]["data"]["reason"] == "timeout", "timeout -> enroll.abort reason=timeout")

        # 依赖状态：只记变化
        d = make(log)
        d.tray, d.sleep_state, d.last_error = None, "awake", ""
        d.runtime = SimpleNamespace(state="ACTIVE")
        d._prepare_ack = lambda: None
        d._refresh_tray = lambda: None
        d.status = lambda: {}
        n0 = len(events(dirpath, "dep.change"))
        d._on_health(Health(True, True))
        d._on_health(Health(True, True))
        first = events(dirpath, "dep.change")[n0:]
        check(len(first) == 1 and first[0]["data"]["initial"] is True and first[0]["level"] == "info", "first poll records the initial state once")
        d._on_health(Health(True, False))
        d._on_health(Health(True, False))
        chg = events(dirpath, "dep.change")[n0 + 1:]
        check(len(chg) == 1 and chg[0]["level"] == "warn" and chg[0]["data"]["dep"] == "tts" and chg[0]["data"]["up"] is False,
              "tts going down: exactly one warn dep.change")
        d._on_health(Health(True, True))
        chg = events(dirpath, "dep.change")[n0 + 2:]
        check(len(chg) == 1 and chg[0]["level"] == "info" and chg[0]["data"]["up"] is True, "tts recovering: one info dep.change")
        d._on_health(Health(False, False))
        check(len(events(dirpath, "dep.change")[n0 + 3:]) == 2, "two deps changing at once -> two events")
    finally:
        log.close()
        elog._default = elog.NullLog()

print("ALL PASS test_p4_enroll_events")
