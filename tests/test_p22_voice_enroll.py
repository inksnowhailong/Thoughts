"""P2.2 B1：守护进程的声纹注册模式接线（/api/speaker 的动作、注册中拒绝 /say、库为空时的备用声纹门）。
不建音频流、不加载模型：用 object.__new__ 拼守护进程 + 假声纹门（不碰任何声纹库文件）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_voice_enroll.py
"""
import queue
import threading

import numpy as np

import guanjia.voice as voice
from guanjia.enroll_session import MIN_TOTAL
from guanjia.events import EventHub
from guanjia.settings import SettingsStore


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


rng = np.random.default_rng(1)


def unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


class FakeGate:
    """只记录调用的假声纹门：库在内存里，不写任何文件。"""

    def __init__(self, people=None, legacy=False, has_backup=True):
        self.people = dict(people or {})
        self.legacy = legacy
        self.has_backup = has_backup
        self.saved = []
        self.threshold = 0.0

    def embed(self, samples):
        return unit(rng.standard_normal(192))

    def refs(self, name):
        return self.people.get(name)

    def names(self):
        return list(self.people)

    def counts(self):
        return {k: len(v) for k, v in self.people.items()}

    def backup(self):
        return None

    def set_person(self, name, embs):
        self.saved.append((name, embs.shape))
        self.people[name] = embs
        self.legacy = False

    def rollback(self):
        if not self.has_backup:
            return False
        self.people, self.legacy = {"我": np.zeros((1, 192), np.float32)}, True
        return True


def make(gate):
    d = object.__new__(voice.VoiceDaemon)
    d._lock, d._stop_evt = threading.Lock(), threading.Event()
    d.state = "LISTENING"
    d.text_q = queue.Queue()
    d._muted, d._paused = threading.Event(), threading.Event()
    d.hub = EventHub()
    d.store = SettingsStore(None)
    d.speaker_gate, d.enroll, d._spare_gate, d.gate = True, None, None, gate
    return d


loud = (0.3 * np.sin(np.arange(16000 * 3) / 7)).astype(np.float32)

# ---------- 1. 完整流程：start → 收样本 → 自测 → 保存 ----------
g = FakeGate({"我": np.stack([unit(rng.standard_normal(192))])}, legacy=True)
d = make(g)
st, body = d._speaker_info({})
check(st == 200 and body["people"] == {"我": 1} and body["legacy"] and body["session"] is None, "info before enrolling (legacy store)")
check(d._speaker_action({"action": "commit"})[0] == 409, "commit without a session refused")
d.state = "SPEAKING"
check(d._speaker_action({"action": "start"})[0] == 409, "cannot start while speaking")
d.state = "LISTENING"
st, body = d._speaker_action({"action": "start", "name": "我"})
check(st == 200 and d.enroll is not None and body["session"]["state"] == "recording", "start enters enroll mode")
check(d._speaker_action({"action": "start"})[0] == 409, "second start refused")
check(d.say("你好") == (False, "enrolling"), "/say refused while enrolling")
check(d._speaker_action({"action": "enough"})[0] == 409, "enough refused before 8 segments")
check(d._speaker_action({"action": "commit"})[0] == 409, "commit refused before self-test")
for _ in range(MIN_TOTAL):
    d.enroll.feed(loud)
st, body = d._speaker_action({"action": "discard_last"})
check(st == 200 and body["session"]["total"] == MIN_TOTAL - 1, "discard_last via the route")
d.enroll.feed(loud)
check(d._speaker_action({"action": "enough"})[0] == 200 and d.enroll.state == "testing", "enough -> testing")
d.enroll.feed(loud)
st, body = d._speaker_info({})
check(body["session"]["state"] == "ready" and body["session"]["test_old_score"] is not None, "info shows ready with old-vs-new scores")
st, body = d._speaker_action({"action": "commit"})
check(st == 200 and g.saved == [("我", (MIN_TOTAL, 192))] and d.enroll is None, "commit writes one person once and leaves enroll mode")
check(d.say("你好") == (True, "queued"), "/say works again after commit")
check(d._speaker_action({"action": "start"})[0] == 409, "cannot start while a reply is queued")
d.text_q.get_nowait()
check(d._speaker_action({"action": "bogus"})[0] == 409, "no session -> bogus action refused")

# ---------- 2. 取消 / 未知动作 ----------
d._speaker_action({"action": "start"})
check(d._speaker_action({"action": "bogus"})[0] == 400, "unknown action refused during a session")
check(d._speaker_action({"action": "cancel"})[0] == 200 and d.enroll is None and len(g.saved) == 1, "cancel leaves the store untouched")
d.speaker_gate = False
check(d._speaker_action({"action": "start"})[0] == 400, "start refused when --no-gate")
d.speaker_gate = True

# ---------- 3. 回滚 ----------
check(d._speaker_action({"action": "rollback"})[0] == 200 and g.legacy, "rollback restores the legacy store")
d._speaker_action({"action": "start"})
check(d._speaker_action({"action": "rollback"})[0] == 409, "rollback refused during a session")
d._speaker_action({"action": "cancel"})
g2 = FakeGate({"我": np.stack([unit(rng.standard_normal(192))])}, has_backup=False)
check(make(g2)._speaker_action({"action": "rollback"})[0] == 404, "rollback without a backup -> 404")

# ---------- 4. 库为空：守护进程没建声纹门，用备用门，保存后接上 ----------
spare = FakeGate()
d = make(None)
d._spare_gate = spare
d._speaker_action({"action": "start", "name": "我"})
for _ in range(MIN_TOTAL):
    d.enroll.feed(loud)
d._speaker_action({"action": "enough"})
d.enroll.feed(loud)
st, body = d._speaker_action({"action": "commit"})
check(st == 200 and d.gate is spare and spare.threshold == d.store.current.spk_threshold, "spare gate becomes the live gate after the first save")

print("ALL PASS test_p22_voice_enroll")
