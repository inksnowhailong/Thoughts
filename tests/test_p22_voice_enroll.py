"""P2.2 B1：守护进程的声纹注册模式接线（/api/speaker 的动作、注册中拒绝 /say、库为空时的备用声纹门、
超时不靠新语音段、前端超时后照常判定、播放重叠的段不收、控制口加锁、保存失败的处理）。
不建音频流、不加载模型：用 object.__new__ 拼守护进程 + 假声纹门（不碰任何声纹库文件）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_voice_enroll.py
"""
import queue
import threading
import time
from collections import deque
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia.enroll_session import MIN_TOTAL, SESSION_TIMEOUT_SEC
from guanjia.events import EventHub
from guanjia.settings import SettingsStore


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


rng = np.random.default_rng(1)
BASE = rng.standard_normal(192)


def unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def same_voice():
    """同一个人的一句话：和 BASE 很像。"""
    return unit(BASE + 0.1 * rng.standard_normal(192))


class FakeGate:
    """只记录调用的假声纹门：库在内存里，不写任何文件。"""

    def __init__(self, people=None, legacy=False, has_backup=True, refs_delay=0.0):
        self.people = dict(people or {})
        self.legacy = legacy
        self.has_backup = has_backup
        self.refs_delay = refs_delay
        self.saved = []
        self.threshold = 0.0
        self.load_error = None
        self.fail = None  # 下一次 save_person 要抛的异常

    def embed(self, samples):
        return same_voice()

    def identify(self, samples):
        return None, 0.5

    def refs(self, name):
        if self.refs_delay:
            time.sleep(self.refs_delay)  # 拉大「检查 → 设置会话」之间的窗口，没锁时两个线程会都成功
        return self.people.get(name)

    def names(self):
        return list(self.people)

    def counts(self):
        return {k: len(v) for k, v in self.people.items()}

    def save_person(self, name, embs):
        if self.fail is not None:
            raise self.fail
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
    d._enroll_lock = threading.Lock()
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
check(st == 200 and body["people"] == {"我": 1} and body["legacy"] and body["session"] is None and body["load_error"] is None,
      "info before enrolling (legacy store)")
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
check(st == 200 and g.saved == [("我", (MIN_TOTAL, 192))] and d.enroll is None, "commit saves one person once and leaves enroll mode")
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

# ---------- 4. 库为空：守护进程没在用声纹门，用备用门，保存后接上 ----------
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

# ---------- 5. 超时不靠新语音段（review 2） ----------
g = FakeGate()
d = make(g)
clock = [0.0]
d._speaker_action({"action": "start"})
d.enroll._now, d.enroll.started = (lambda: clock[0]), 0.0
check(d.say("x") == (False, "enrolling"), "within the timeout /say is still refused")
clock[0] = SESSION_TIMEOUT_SEC + 1  # 假时钟推过 600 秒，期间一段话都没来
check(d.say("x") == (True, "queued") and d.enroll is None, "past the timeout say() works without any new segment")
d.text_q.get_nowait()
d._speaker_action({"action": "start"})
d.enroll._now, d.enroll.started = (lambda: clock[0]), clock[0]
clock[0] += SESSION_TIMEOUT_SEC + 1
check(d._speaker_info({})[1]["session"] is None and d.enroll is None, "info also notices the timeout")
d._speaker_action({"action": "start"})
d.enroll._now, d.enroll.started = (lambda: clock[0]), clock[0]
clock[0] += SESSION_TIMEOUT_SEC + 1
check(d._speaker_action({"action": "commit"})[0] == 409 and d.enroll is None, "actions notice the timeout (no session)")

# ---------- 6. 前端：超时后的这一段照常走 ASR/判定，不被丢掉（review 2） ----------
heard = []
d = make(g)
d.aec, d.down, d.mic_gain = None, SimpleNamespace(process=lambda x: x), 1.0
d._raw_ring, d._vad_pos, d._was_speech, d._preroll = deque(maxlen=10), 0, False, deque(maxlen=50)
d._play_buf, d._play_pos, d.play_q = np.zeros(0, dtype=np.float32), 0, queue.Queue()
seg = SimpleNamespace(duration=2.0, samples=loud, start=100.0)
d.vad = SimpleNamespace(push=lambda y: None, is_speech=lambda: False, pop_segments=lambda: [seg])
d._asr_stream = object()
d.asr = SimpleNamespace(new_stream=lambda: object(), feed=lambda *a: None, finish=lambda s: None, text=lambda s: "开灯")
d.asr_final = SimpleNamespace(transcribe=lambda samples, fallback: "开灯")
d._dump_segment = lambda seg, text: None
d._barge_step = lambda rms, playing: None
d._on_heard = lambda text, score, **kw: heard.append((text, score))
frame = voice.Frames(mic=np.zeros(160, dtype=np.float32), ref=np.zeros(160, dtype=np.float32), playing=False)
clock = [0.0]
d._speaker_action({"action": "start"})
d.enroll._now, d.enroll.started = (lambda: clock[0]), 0.0
d._frontend_step(frame)
check(heard == [] and d.enroll is not None and d.enroll.snapshot()["total"] == 1, "while enrolling the segment is taken, not judged")
clock[0] = SESSION_TIMEOUT_SEC + 1
d._frontend_step(frame)
check(heard == [("开灯", 0.5)] and d.enroll is None, "after the timeout the next segment goes through ASR and judging as usual")

# ---------- 7. 和播放重叠的段不收（review 8） ----------
d = make(g)
d._play_buf, d._play_pos, d.play_q = np.zeros(0, dtype=np.float32), 0, queue.Queue()
d._speaker_action({"action": "start"})
d._last_play_t = 5.0
r = d._enroll_take(SimpleNamespace(samples=loud, start=5.2))
check(r and d.enroll.snapshot()["total"] == 0 and "出声" in d.enroll.last["reason"], "segment starting 0.2s after playback is refused")
d._enroll_take(SimpleNamespace(samples=loud, start=5.4))
check(d.enroll.snapshot()["total"] == 1, "segment starting 0.4s after playback is accepted")
frame_play = voice.Frames(mic=np.zeros(160, dtype=np.float32), ref=np.zeros(160, dtype=np.float32), playing=True)
d.aec, d.down, d.mic_gain = None, SimpleNamespace(process=lambda x: np.zeros(160, dtype=np.float32)), 1.0
d._raw_ring, d._vad_pos, d._was_speech, d._preroll = deque(maxlen=10), 16000 * 7, False, deque(maxlen=50)
d.vad = SimpleNamespace(push=lambda y: None, is_speech=lambda: False, pop_segments=lambda: [])
d._asr_stream = object()
d.asr = SimpleNamespace(new_stream=lambda: object(), feed=lambda *a: None)
d._barge_step = lambda rms, playing: None
d._frontend_step(frame_play)
check(abs(d._last_play_t - 7.01) < 1e-6, "front thread remembers the VAD time of the last audible playback frame")

# ---------- 8. 控制口加锁：两个线程同时 start，恰好一个成功（review 4） ----------
slow = FakeGate(refs_delay=0.1)
d = make(slow)
res = []
barrier = threading.Barrier(2)


def go():
    barrier.wait()
    res.append(d._speaker_action({"action": "start"})[0])


ts = [threading.Thread(target=go) for _ in range(2)]
[t.start() for t in ts]
[t.join() for t in ts]
check(sorted(res) == [200, 409], f"two concurrent starts: exactly one wins {res}")

# ---------- 9. 保存失败 / 库读不出：返回错误，库不切换，会话保留（review 7、9） ----------
g = FakeGate({"我": np.stack([unit(rng.standard_normal(192))])}, legacy=True)
d = make(g)
d._speaker_action({"action": "start"})
for _ in range(MIN_TOTAL):
    d.enroll.feed(loud)
d._speaker_action({"action": "enough"})
d.enroll.feed(loud)
g.fail = PermissionError("denied")
st, body = d._speaker_action({"action": "commit"})
check(st == 500 and "写声纹库失败" in body["reason"] and "denied" in body["reason"], "commit with PermissionError -> 500 with a Chinese reason")
check(d.enroll is not None and d.enroll.state == "ready" and g.legacy and g.saved == [], "failed save keeps the session and the old store")
g.fail = ValueError("声纹库读不出来（坏了），先回滚或处理文件再注册")
st, body = d._speaker_action({"action": "commit"})
check(st == 409 and "读不出来" in body["reason"] and d.enroll is not None, "unreadable store -> commit refused with the reason")
g.load_error = "坏了"
check(d._speaker_info({})[1]["load_error"] == "坏了", "/api/speaker reports load_error")
g.fail = None
st, body = d._speaker_action({"action": "commit"})
check(st == 200 and len(g.saved) == 1, "retry after the cause is gone succeeds")

print("ALL PASS test_p22_voice_enroll")
