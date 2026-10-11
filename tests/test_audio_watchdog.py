"""输入流看门狗用例（假流 + 假时钟，不碰声卡、不加载模型）：
  1. InputWatchdog 判据：回调停了 / 流不活跃 / 播放时输入恒为数字零；安静（全零无播放）永不触发
  2. StreamGuard：重开流、关旧流卡死、重开失败、频率上限、播放中延后、游戏态不管、launch_app 后自检
  3. VoiceDaemon 接线：回调喂看门狗、launch_app 触发自检、status 带 audio、run() 里流死了会重开/重开失败退 3

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_audio_watchdog.py
"""
import queue
import threading
import time
from collections import deque
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia.audio_watchdog import InputWatchdog, StreamGuard
from guanjia.barge import BargeConfig, BargeDetector
from guanjia.events import EventHub
from guanjia.settings import SettingsStore
from guanjia.status import Health
from guanjia.wake import WakeMatcher


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def feed(wd, clk, seconds, *, signal=False, voiced=False, step=0.01) -> None:
    """按 10ms 一帧喂 seconds 秒回调心跳。"""
    for _ in range(int(round(seconds / step))):
        clk.t += step
        wd.beat(signal, voiced)


# ---------- 1. InputWatchdog ----------
clk = Clock()
wd = InputWatchdog(clock=clk)
check(wd.check(True) is None, "not armed -> never triggers")
wd.arm()
feed(wd, clk, 60, signal=True)
check(wd.check(True) is None, "healthy callbacks with signal -> ok")

clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
feed(wd, clk, 600, signal=False, voiced=False)
check(wd.check(True) is None, "10 min of quiet (digital zero, nothing playing, callbacks alive) never triggers")

wd = InputWatchdog(clock=clk)
wd.arm()
feed(wd, clk, 5.9, signal=False, voiced=True)
check(wd.check(True) is None, "5.9 s of zero input while playing is still ok")
feed(wd, clk, 0.2, signal=False, voiced=True)
v = wd.check(True)
check(v is not None and v["why"] == "zero_during_playback", "6 s of zero input while we are speaking -> zero_during_playback")
wd.arm()
feed(wd, clk, 5.0, signal=False, voiced=True)
wd.beat(True, True)
feed(wd, clk, 5.0, signal=False, voiced=True)
check(wd.check(True) is None, "one non-zero input frame resets the zero-while-playing run")
wd.arm()
feed(wd, clk, 5.0, signal=False, voiced=True)
feed(wd, clk, 30.0, signal=False, voiced=False)
feed(wd, clk, 0.9, signal=False, voiced=True)
check(wd.check(True) is None, "zero run is cumulative over voiced frames only: a long silent gap neither resets nor adds (5.9 s total)")
feed(wd, clk, 0.2, signal=False, voiced=True)
check(wd.check(True)["why"] == "zero_during_playback", "...and crosses 6 s once more voiced zero frames arrive")

clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
feed(wd, clk, 1, signal=True)
clk.t += 4.9
check(wd.check(True) is None, "4.9 s without a callback is not yet a stall")
clk.t += 0.2
v = wd.check(True)
check(v is not None and v["why"] == "callback_stall" and v["cb_age"] >= 5.0, "5 s without callbacks -> callback_stall with age")
check(v["frames"] == 100, "verdict carries frames delivered since arm")
check(wd.check(False)["why"] == "stream_inactive", "stream not active -> stream_inactive")

wd.disarm()
clk.t += 500
check(wd.check(True) is None and wd.check(False) is None, "disarmed (gaming) -> never triggers")
wd.arm()
check(wd.check(True) is None, "re-arm re-baselines the clock")
clk.t += 1
check(wd.check(True) is None, "1 s after arm is fine")

# 自检：launch_app 后 15 秒
clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
mark = wd.mark()
feed(wd, clk, 15, signal=False)
check(wd.selfcheck(mark, True) is None, "self-check: 15 s of live callbacks -> ok even if all zero")
mark = wd.mark()
clk.t += 15
v = wd.selfcheck(mark, True)
check(v is not None and v["why"] == "callback_stall", "self-check: no callbacks for 15 s -> callback_stall (threshold 2 s, tighter than the watchdog)")
mark = wd.mark()
feed(wd, clk, 3, signal=False)  # 15 秒里只来了 3 秒的帧：慢流
clk.t += 12
wd.last_beat = clk.t - 0.5  # 最后一次心跳很新鲜，只是整体帧率太低
v = wd.selfcheck(mark, True)
check(v is not None and v["why"] == "callback_slow", "self-check: frame rate under half of expected -> callback_slow")
mark = wd.mark()
clk.t += 3
check(wd.selfcheck(mark, True) is None, "self-check window shorter than 5 s is not judged")
mark = wd.mark()
wd.arm()
clk.t += 20
check(wd.selfcheck(mark, True) is None, "self-check from before a re-arm (stream was reopened) is dropped")


# ---------- 2. StreamGuard ----------
class FakeStream:
    made: list = []

    def __init__(self, name="stream", hang=None, fail_enter=False) -> None:
        self.name, self.active, self.closed, self.hang, self.fail_enter = name, False, False, hang, fail_enter
        FakeStream.made.append(self)

    def __enter__(self):
        if self.fail_enter:
            raise OSError("device gone")
        self.active = True
        return self

    def __exit__(self, *a):
        if self.hang is not None:
            self.hang.wait(10)
        self.active, self.closed = False, True
        return False


def build(clk, *, streams=None, max_reopens=3, window_sec=600.0, close_timeout=0.2, refresh=None):
    FakeStream.made = []
    events, notes = [], []
    wd = InputWatchdog(clock=clk)
    queue_of = list(streams or [])

    def make():
        return queue_of.pop(0) if queue_of else FakeStream("auto")

    def emit(comp, ev, msg="", **kw):
        events.append((comp, ev, msg, kw))

    g = StreamGuard(make, wd, emit=emit, describe=lambda: {"in_name": "Halo mic", "out_name": "Halo spk"},
                    refresh=refresh, on_event=lambda kind, data: notes.append(kind), clock=clk, max_reopens=max_reopens,
                    window_sec=window_sec, close_timeout=close_timeout)
    return g, wd, events, notes


clk = Clock()
g, wd, events, notes = build(clk)
with g:
    first = g.stream
    check(first.active and wd.armed, "guard opens and arms the watchdog")
    feed(wd, clk, 10, signal=True)
    check(g.tick(paused=False, busy=False) == "ok" and g.stream is first, "healthy stream -> ok, untouched")
    clk.t += 6
    check(g.tick(paused=False, busy=False) == "reopened", "callback stall -> stream reopened")
    check(g.stream is not first and first.closed and g.stream.active, "old stream closed, new stream opened and running")
    evs = [e[1] for e in events]
    check(evs == ["audio.stall", "audio.reopen"], f"events: audio.stall then audio.reopen (got {evs})")
    stall = events[0]
    check(stall[3]["why"] == "callback_stall" and stall[3]["in_name"] == "Halo mic" and stall[3]["trigger"] == "watchdog"
          and stall[0] == "audio" and stall[3].get("level") == "warn", "audio.stall carries criterion, device names, trigger, warn level")
    check(events[1][3]["in_name"] == "Halo mic" and events[1][3]["closed_clean"] is True and events[1][3]["attempt"] == 1,
          "audio.reopen carries device and attempt")
    check(notes == ["stall", "reopened"], "tray hook notified")
    check(g.tick(paused=False, busy=False) == "ok", "after reopen the watchdog is re-baselined (no instant re-trigger)")
    info = g.info()
    check(info["reopens"] == 1 and info["last"]["why"] == "callback_stall", "info() exposes reopen count and last stall for status/tray")
check(g.stream is None or g.stream.closed, "leaving the guard closes the stream")

# 关旧流卡死：不能卡住主循环，也不能 refresh（PortAudio 重初始化）
clk = Clock()
gate = threading.Event()
hung = FakeStream("hung", hang=gate)
refreshed = []
g, wd, events, notes = build(clk, streams=[hung], refresh=lambda: refreshed.append(1))
with g:
    clk.t += 6
    t0 = time.monotonic()
    r = g.tick(paused=False, busy=False)
    took = time.monotonic() - t0
    check(r == "reopened" and took < 2.0, f"hung close of the old stream is abandoned after the timeout (took {took:.2f}s)")
    check(events[-1][3]["closed_clean"] is False and not refreshed, "hung close -> closed_clean false, PortAudio is not re-initialised")
    gate.set()

# 正常关闭才重初始化 PortAudio（拿到新的默认设备）
clk = Clock()
refreshed = []
g, wd, events, notes = build(clk, refresh=lambda: refreshed.append(1))
with g:
    clk.t += 6
    g.tick(paused=False, busy=False)
    check(refreshed == [1], "clean close -> PortAudio re-initialised before reopening")

# 重开失败
clk = Clock()
g, wd, events, notes = build(clk, streams=[FakeStream("ok")])
with g:
    clk.t += 6
    g._make = lambda: FakeStream("bad", fail_enter=True)
    r = g.tick(paused=False, busy=False)
    check(r == "failed", "reopen failure -> tick returns failed (caller exits with the restart code)")
    check(events[-1][1] == "audio.reopen_failed" and events[-1][3]["level"] == "error" and "device gone" in events[-1][3]["error"],
          "audio.reopen_failed (error) carries the reason")
    check(notes[-1] == "failed", "tray hook told about the failure")

# 频率上限：10 分钟内最多重开 3 次，第 4 次直接 failed
clk = Clock()
g, wd, events, notes = build(clk, max_reopens=3, window_sec=600.0)
with g:
    for i in range(3):
        clk.t += 6
        check(g.tick(paused=False, busy=False) == "reopened", f"reopen #{i + 1} inside the window")
    clk.t += 6
    check(g.tick(paused=False, busy=False) == "failed" and events[-1][3]["reason"] == "too_many",
          "4th stall inside 10 min -> failed(too_many), let the supervisor restart the whole process")
clk = Clock()
g, wd, events, notes = build(clk, max_reopens=3, window_sec=600.0)
with g:
    for i in range(3):
        clk.t += 6
        g.tick(paused=False, busy=False)
    clk.t += 700
    check(g.tick(paused=False, busy=False) == "reopened", "once the window has passed the budget is back")

# 播放中的"零输入"延后到播完再重开；回调停了不管播不播都立刻重开
clk = Clock()
g, wd, events, notes = build(clk)
with g:
    feed(wd, clk, 7, signal=False, voiced=True)
    check(g.tick(paused=False, busy=True) == "deferred" and not events, "zero_during_playback while speaking -> deferred, nothing logged yet")
    check(g.tick(paused=False, busy=False) == "reopened" and events[0][3]["why"] == "zero_during_playback", "after it finished speaking -> reopened")
clk = Clock()
g, wd, events, notes = build(clk)
with g:
    clk.t += 6
    check(g.tick(paused=False, busy=True) == "reopened", "callback stall while speaking -> reopened at once (nothing is being played anyway)")

# 游戏态 / 静音期间的停流不算故障
clk = Clock()
g, wd, events, notes = build(clk)
with g:
    clk.t += 600
    check(g.tick(paused=True, busy=False) == "ok" and not events, "paused (gaming) -> never judged")

# launch_app 后 15 秒自检
clk = Clock()
g, wd, events, notes = build(clk)
with g:
    g.request_selfcheck("launch_app", delay=15.0)
    feed(wd, clk, 10, signal=False)
    check(g.tick(paused=False, busy=False) == "ok" and not events, "self-check not due yet")
    feed(wd, clk, 6, signal=False)
    check(g.tick(paused=False, busy=False) == "ok", "self-check passes on live (even all-zero) callbacks")
    check([e[1] for e in events] == ["audio.selfcheck"] and events[0][3]["ok"] is True and events[0][3]["reason"] == "launch_app",
          "self-check ok recorded as audio.selfcheck")
    check(g.tick(paused=False, busy=False) == "ok" and len(events) == 1, "self-check fires only once")
clk = Clock()
g, wd, events, notes = build(clk)
with g:
    first = g.stream
    g.request_selfcheck("launch_app", delay=15.0)
    clk.t += 3
    wd.last_beat = clk.t - 0.1  # 刚有心跳，看门狗 5 秒线没到
    clk.t += 13
    wd.last_beat = clk.t - 2.6  # 但已 2.6 秒没帧：自检的 2 秒线先到
    check(g.tick(paused=False, busy=False) == "reopened" and g.stream is not first, "self-check catches a stall the 5 s watchdog has not yet")
    check(events[0][1] == "audio.stall" and events[0][3]["trigger"] == "launch_app", "stall event is tagged with the launch_app trigger")
clk = Clock()
g, wd, events, notes = build(clk)
with g:
    g.request_selfcheck("launch_app", delay=15.0)
    clk.t += 20
    check(g.tick(paused=True, busy=False) == "ok" and not events, "self-check dropped when the game put us in gaming state")
    check(g.tick(paused=False, busy=False) in ("ok", "reopened") and "audio.selfcheck" not in [e[1] for e in events],
          "dropped self-check does not come back")


# ---------- 3. VoiceDaemon 接线 ----------
def make():
    d = object.__new__(voice.VoiceDaemon)
    d._lock, d._stop_evt = threading.Lock(), threading.Event()
    d._gen, d.state, d.sr, d.frame = 0, "LISTENING", 16000, 160
    d.play_q, d.text_q, d.in_q = queue.Queue(), queue.Queue(), queue.Queue()
    d._muted, d._paused = threading.Event(), threading.Event()
    d._play_buf, d._play_pos, d._buf_gen, d._played_gen = np.zeros(0, dtype=np.float32), 0, 0, -1
    d._earcon = np.ones(4, dtype=np.float32)
    d._turn_first, d._turn_t0 = False, 0.0
    d.tray, d.last_error, d.sleep_state, d.gate_mode = None, "", "awake", "enforce"
    d.tts = SimpleNamespace(abort=lambda: None)
    d.brain = SimpleNamespace(last_run_id=None)
    d.health = Health(True, True)
    d.runtime = SimpleNamespace(state="ACTIVE", start=lambda: None, stop=lambda: None)
    d._exit_code = None
    d.hub = EventHub()
    d.store = SettingsStore(None)
    d.wake = WakeMatcher(("管家",))
    d.gate = None
    d.barge = BargeDetector(BargeConfig())
    d._ack_text = ""
    d._followup_until = 0.0
    d._noise, d._noise_pos = np.zeros(16000, dtype=np.float32), 0
    d._play_events, d._dropped, d._underflows = deque(maxlen=10), 0, 0
    return d


clk = Clock()
d = make()
d.watchdog = InputWatchdog(clock=clk)
d.watchdog.arm()
outdata = np.zeros((160, 1), dtype=np.float32)
status = SimpleNamespace(output_underflow=False)
d._callback(np.zeros((160, 1), dtype=np.float32), outdata, 160, None, status)
check(d.watchdog.count == 1 and d.watchdog.last_signal == d.watchdog.armed_at, "audio callback beats the watchdog (zero input -> no signal)")
d._callback(np.full((160, 1), 0.001, dtype=np.float32), outdata, 160, None, status)
check(d.watchdog.count == 2 and d.watchdog.last_signal >= d.watchdog.armed_at, "non-zero input counts as signal")
d.play_q.put((0, np.full(1600, 0.1, dtype=np.float32)))
for _ in range(5):
    clk.t += 0.01
    d._callback(np.zeros((160, 1), dtype=np.float32), outdata, 160, None, status)
check(abs(d.watchdog.zero_play - 0.05) < 1e-9, "zero input while we output real speech accumulates zero_play")

d = make()  # 没建看门狗（旧用例拼的半个守护进程）：回调照常
d._callback(np.zeros((160, 1), dtype=np.float32), outdata, 160, None, status)
check(d.in_q.qsize() == 1, "callback works without a watchdog (half-built daemon in old tests)")
check(d.status()["audio"] is None, "status() has an audio field (None before the stream opens)")

requests = []
d.guard = SimpleNamespace(request_selfcheck=lambda reason, delay: requests.append((reason, delay)),
                          info=lambda: {"reopens": 2, "last": {"why": "callback_stall"}})
d._on_tool({"tool": "exec", "level": "L2", "decision": "allow", "summary": "x", "state": "done"})
check(requests == [], "ordinary tools do not trigger a self-check")
d._on_tool({"tool": "launch_app", "level": "L1", "decision": "allow", "summary": 'launch_app: {"name":"lol"}', "state": "done"})
check(requests == [("launch_app", voice.LAUNCH_SELFCHECK_SEC)] and 10 <= voice.LAUNCH_SELFCHECK_SEC <= 20,
      "launch_app schedules an input-stream self-check 10~20 s later")
check(d.status()["audio"] == {"reopens": 2, "last": {"why": "callback_stall"}}, "status() exposes guard info")
d._on_audio_event("reopened", {"why": "callback_stall"})
check("麦克风" in d.last_error, "reopen shows on the tray as a (clearable) error text")


# run()：流死了会重开；重开失败退出码 3
class DyingStream:
    """第一个实例启动后立刻变成不活跃（模拟流悄悄死掉）；quit 时才退出。"""
    made: list = []

    def __init__(self, **kw) -> None:
        self.latency, self.active, self.closed = (0.01, 0.01), False, False
        DyingStream.made.append(self)

    def __enter__(self):
        self.active = len(DyingStream.made) > 1
        return self

    def __exit__(self, *a):
        self.active, self.closed = False, True
        return False


def run_daemon(make_stream, wait=0.8) -> tuple[int, "voice.VoiceDaemon"]:
    voice.sd.Stream = make_stream
    voice.start_control_server = lambda api, port, panel=None: SimpleNamespace(shutdown=lambda: None)
    voice.VoiceDaemon._refresh_audio = lambda self: None  # 不碰真 PortAudio
    d = make()
    d._audio_devices = lambda: {"in_name": "fake mic", "out_name": "fake spk"}
    d.in_dev = d.out_dev = None
    d.mic_gain, d.aec, d.control_port, d.tray_on, d.panel = 1.0, None, 0, False, None
    d.monitor = d.audit = SimpleNamespace(start=lambda: None, stop=lambda: None)
    d._front_dirty, d._turn_marks = False, None
    codes = []

    def target():
        try:
            d.run()
        except SystemExit as e:
            codes.append(e.code)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    time.sleep(wait)
    if t.is_alive():
        d._panel_control("quit")
    t.join(10)
    return (codes[0] if codes else -99), d


DyingStream.made = []
code, d = run_daemon(DyingStream, wait=1.5)
check(len(DyingStream.made) == 2 and DyingStream.made[0].closed, "run(): a stream that goes inactive is reopened (2nd stream made, 1st closed)")
check(code == 0, f"run(): after a successful reopen a normal quit still exits 0 (got {code})")


class AlwaysDead(DyingStream):
    def __enter__(self):
        self.active = False
        if len(DyingStream.made) > 1:
            raise OSError("no device")
        return self


DyingStream.made = []
code, d = run_daemon(AlwaysDead, wait=3.0)
check(code == voice.EXIT_RESTART, f"run(): reopen failure -> exit code 3 so the supervisor restarts us (got {code})")
print("ALL PASS test_audio_watchdog")
