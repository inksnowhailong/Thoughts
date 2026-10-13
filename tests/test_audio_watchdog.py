"""输入流看门狗 v2 用例（假流 + 假时钟，不碰声卡、不加载模型）：
  1. InputWatchdog：只有「确认的回调停摆」才给判定；安静 / 播放 / 全零都不触发；全零出声只报告不判停摆
  2. AudioGuard：停摆 -> "stall" + audio.stall 事件；全零出声只记 audio.zero_playback（限频）；launch_app 自检只记不动作；游戏态不管
  3. VoiceDaemon 接线：回调喂心跳、launch_app 触发自检、status 带 audio、停流/开流时 disarm/arm
  4. run()（假流）：
     - 回归（6804b86 事故）：主循环里有「播放开始」事件时进程不能崩（v1 把 guard 变量 g 覆盖成了整数 -> AttributeError -> 退出码 1）
     - 确认停摆 -> 退出码 3 + voice.exit(audio_callback_stall) + WARNING；关流卡死 -> 保险定时器强制以同一退出码退出
     - 游戏态（流本来就停着）永远不判停摆

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_audio_watchdog.py
"""
import json
import logging
import queue
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia import elog
from guanjia.audio_watchdog import AudioGuard, InputWatchdog
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
check(wd.check() is None, "not armed -> never triggers")
wd.arm()
feed(wd, clk, 60, signal=True)
check(wd.check() is None, "healthy callbacks with signal -> ok")

clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
feed(wd, clk, 600, signal=False, voiced=False)
check(wd.check() is None and wd.zero_playback() is None, "10 min of quiet (digital zero, nothing playing, callbacks alive) never triggers")

# 回调停摆：>5s 无回调 + 复核仍没动才确认
clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
feed(wd, clk, 3, signal=True)
clk.t += 4.9
check(wd.check() is None, "4.9 s without callbacks is not a stall yet")
clk.t += 0.2
check(wd.check() is None, "5.1 s: first sighting only starts the confirmation timer (no verdict yet)")
clk.t += 0.5
check(wd.check() is None, "0.5 s into confirmation: still no verdict")
clk.t += 0.6
v = wd.check()
check(v is not None and v["why"] == "callback_stall" and v["cb_age"] > 6 and v["frames"] == 300,
      "stall confirmed after 5 s + 1 s with an unchanged callback count, with evidence")

# 期间回调动过（睡眠恢复 / 主线程被卡后的时钟跳变）：不判，重新计
clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
clk.t += 8.0
check(wd.check() is None, "suspect recorded")
wd.beat(True, False)
clk.t += 2.0
check(wd.check() is None, "a callback arrived during confirmation -> suspicion cleared, no verdict")
clk.t += 4.0
check(wd.check() is None, "...and the 5 s clock restarts from the last callback")

# 游戏态停流
clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
clk.t += 100
check(wd.check() is None, "first sighting")
wd.disarm()
clk.t += 100
check(wd.check() is None, "disarmed (gaming) -> never judged")
wd.arm()
check(wd.check() is None, "re-arm resets the baseline")

# 出声时输入全零：只报告，不判停摆；一段全零只报一次，见到信号后才会再报
clk = Clock()
wd = InputWatchdog(clock=clk)
wd.arm()
feed(wd, clk, 5.9, signal=False, voiced=True)
check(wd.zero_playback() is None, "5.9 s of zero input while playing: nothing yet")
feed(wd, clk, 0.2, signal=False, voiced=True)
z = wd.zero_playback()
check(z is not None and z["why"] == "zero_during_playback" and wd.check() is None, "6 s zero while speaking -> zero_playback report, but check() stays None")
feed(wd, clk, 5.0, signal=False, voiced=True)
check(wd.zero_playback() is None, "same zero run is reported only once")
wd.beat(True, True)
feed(wd, clk, 6.5, signal=False, voiced=True)
check(wd.zero_playback() is not None, "after a non-zero frame the next zero run is reported again")
wd.arm()
feed(wd, clk, 5.0, signal=False, voiced=True)
wd.beat(True, True)
feed(wd, clk, 5.0, signal=False, voiced=True)
check(wd.zero_playback() is None, "one non-zero input frame resets the zero-while-playing run")
wd.arm()
feed(wd, clk, 5.0, signal=False, voiced=True)
feed(wd, clk, 30.0, signal=False, voiced=False)
feed(wd, clk, 0.9, signal=False, voiced=True)
check(wd.zero_playback() is None, "zero run counts voiced frames only: a silent gap neither resets nor adds (5.9 s)")


# ---------- 2. AudioGuard ----------
def build(clk, **kw):
    events = []
    wd = InputWatchdog(clock=clk)
    wd.arm()
    g = AudioGuard(wd, emit=lambda comp, ev, msg="", **k: events.append((comp, ev, msg, k)), clock=clk,
                   describe=lambda: {"in_name": "fake mic", "out_name": "fake spk"}, **kw)
    return g, wd, events


clk = Clock()
g, wd, events = build(clk)
feed(wd, clk, 30, signal=True)
check(g.tick(False) == "ok" and not events, "healthy -> ok, no events")
clk.t += 6
check(g.tick(False) == "ok" and not events, "first sighting of a stall -> still ok (confirming)")
clk.t += 1.2
check(g.tick(False) == "stall", "confirmed stall -> 'stall'")
check([e[1] for e in events] == ["audio.stall"] and events[0][3]["level"] == "error" and events[0][3]["action"] == "restart"
      and events[0][3]["in_name"] == "fake mic" and events[0][3]["why"] == "callback_stall",
      "audio.stall event: error level, action=restart, evidence + device names")
check(g.info()["last_stall"]["why"] == "callback_stall" and g.info()["armed"] is True, "info() exposes the last confirmed stall")

clk = Clock()
g, wd, events = build(clk)
clk.t += 600
check(g.tick(True) == "ok" and not events, "paused (gaming) -> never judged")
check(g.tick(False) == "ok", "...and unpausing without a fresh arm() only starts the confirmation (no instant exit)")

# 全零出声：只记事件，限频
clk = Clock()
g, wd, events = build(clk)
feed(wd, clk, 6.5, signal=False, voiced=True)
check(g.tick(False) == "ok", "zero-while-playing never makes tick() ask for a restart")
check([e[1] for e in events] == ["audio.zero_playback"] and events[0][3]["action"] == "none" and events[0][3]["level"] == "warn",
      "zero-while-playing -> one audio.zero_playback warn event, action=none")
wd.beat(True, True)
feed(wd, clk, 6.5, signal=False, voiced=True)
g.tick(False)
check(len(events) == 1, "second zero run within the rate-limit gap is not logged again")
clk.t += 301
wd.beat(True, True)
feed(wd, clk, 6.5, signal=False, voiced=True)
g.tick(False)
check(len(events) == 2, "...but after the gap it is")

# launch_app 自检：只记不动作
clk = Clock()
g, wd, events = build(clk)
g.request_selfcheck("launch_app", delay=15.0)
feed(wd, clk, 10, signal=False)
check(g.tick(False) == "ok" and not events, "self-check not due yet")
feed(wd, clk, 6, signal=False)
check(g.tick(False) == "ok", "self-check never asks for a restart")
check([e[1] for e in events] == ["audio.selfcheck"] and events[0][3]["ok"] is True and events[0][3]["reason"] == "launch_app"
      and events[0][3]["level"] == "info" and events[0][3]["rate"] > 0.9, "healthy self-check recorded as ok, rate ~1")
g.tick(False)
check(len(events) == 1, "self-check fires only once")
clk = Clock()
g, wd, events = build(clk)
g.request_selfcheck("launch_app", delay=15.0)
feed(wd, clk, 3, signal=True)
clk.t += 4.0  # 回调 4 秒没来：不到 5 秒停摆线，但自检看得出
wd.last_beat = clk.t - 3.0
clk.t += 9
wd.last_beat = clk.t - 3.0
check(g.tick(False) == "ok" and events and events[-1][1] == "audio.selfcheck" and events[-1][3]["ok"] is False
      and events[-1][3]["level"] == "warn", "stalled-ish self-check is recorded as a warn, still no action")
clk = Clock()
g, wd, events = build(clk)
g.request_selfcheck("launch_app", delay=15.0)
clk.t += 20
check(g.tick(True) == "ok" and not events, "self-check dropped when gaming starts")
check(g.tick(False) == "ok" and not any(e[1] == "audio.selfcheck" for e in events), "dropped self-check does not come back")

# 回归（2026-10-12/13 两次崩溃）：线上 describe() 带 "rate"（设备采样率），自检结果也有 "rate"（实际帧率/期望帧率），
# 两个 ** 展开撞名 → TypeError 让语音进程退出。自检事件里设备采样率改叫 device_rate，rate 仍是帧率比。
clk = Clock()
events = []
wd = InputWatchdog(clock=clk)
wd.arm()
g = AudioGuard(wd, emit=lambda comp, ev, msg="", **k: events.append((comp, ev, msg, k)), clock=clk,
               describe=lambda: {"in_name": "fake mic", "out_name": "fake spk", "rate": 16000})
g.request_selfcheck("launch_app", delay=15.0)
feed(wd, clk, 16, signal=False)
try:
    r = g.tick(False)
except TypeError as e:
    r = f"TypeError: {e}"
check(r == "ok" and [e[1] for e in events] == ["audio.selfcheck"] and 0.9 < events[0][3]["rate"] <= 1.1
      and events[0][3]["device_rate"] == 16000 and events[0][3]["in_name"] == "fake mic",
      f"self-check with a describe() that has 'rate' no longer crashes: {r}")


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
    d.tts = SimpleNamespace(abort=lambda: None, control=lambda *a: None)
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
                          info=lambda: {"cb_age": 0.01, "last_stall": None})
d._on_tool({"tool": "exec", "level": "L2", "decision": "allow", "summary": "x", "state": "done"})
check(requests == [], "ordinary tools do not trigger a self-check")
d._on_tool({"tool": "launch_app", "level": "L1", "decision": "allow", "summary": 'launch_app: {"name":"lol"}', "state": "done"})
check(requests == [("launch_app", voice.LAUNCH_SELFCHECK_SEC)] and 10 <= voice.LAUNCH_SELFCHECK_SEC <= 20,
      "launch_app schedules an input-stream self-check 10~20 s later")
check(d.status()["audio"] == {"cb_age": 0.01, "last_stall": None}, "status() exposes guard info")

# 游戏态停流 / 开流：看门狗 disarm / arm
d = make()
d.watchdog = InputWatchdog()
d.watchdog.arm()
fs = SimpleNamespace(stop=lambda: None, start=lambda: None)
d._suspend_audio(fs)
check(d.watchdog.armed is False, "entering gaming disarms the watchdog (no callbacks while the stream is stopped)")
voice.StreamingASR, voice.FinalASR = lambda: None, lambda: None  # 开流只是重建 ASR，这里不加载模型
d._reset_frontend = lambda: None
d._resume_audio(fs)
check(d.watchdog.armed is True and d.watchdog.count == 0, "leaving gaming re-arms the watchdog from the restart moment")


# ---------- 4. run()：假流 ----------
class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class FakeStream:
    """假的全双工流。beats=True 时开一个线程模拟回调心跳（每 10ms 一次）；hang_exit=Event 时关流会卡在这个事件上。"""
    beats = True
    hang_exit: threading.Event | None = None
    made: list = []
    daemon = None

    def __init__(self, **kw) -> None:
        self.latency, self.active, self.closed = (0.01, 0.01), False, False
        FakeStream.made.append(self)

    def __enter__(self):
        self.active = True
        if FakeStream.beats:
            threading.Thread(target=self._beat, daemon=True).start()
        return self

    def _beat(self) -> None:
        while self.active and not self.closed:
            if FakeStream.daemon.watchdog is not None:
                FakeStream.daemon.watchdog.beat(True, False)
            time.sleep(0.01)

    def stop(self) -> None:
        self.active = False

    def start(self) -> None:
        self.active = True

    def __exit__(self, *a):
        self.active = False
        if FakeStream.hang_exit is not None:
            FakeStream.hang_exit.wait(30)
        self.closed = True
        return False


def run_daemon(*, wait, beats=True, hang_exit=None, paused=False, play_event=False, stall_sec=0.4, confirm_sec=0.3,
               quit_after=True):
    FakeStream.beats, FakeStream.hang_exit, FakeStream.made = beats, hang_exit, []
    voice.sd.Stream = FakeStream
    voice.start_control_server = lambda api, port, panel=None: SimpleNamespace(shutdown=lambda: None)
    voice.InputWatchdog = lambda: InputWatchdog(stall_sec=stall_sec, confirm_sec=confirm_sec)
    d = make()
    FakeStream.daemon = d
    d._audio_devices = lambda: {"in_name": "fake mic", "out_name": "fake spk"}
    d.in_dev = d.out_dev = None
    d.mic_gain, d.aec, d.control_port, d.tray_on, d.panel = 1.0, None, 0, False, None
    d.monitor = d.audit = SimpleNamespace(start=lambda: None, stop=lambda: None)
    d._front_dirty, d._turn_marks = False, None
    if paused:
        d._paused.set()
    if play_event:
        d._play_events.append((time.time(), 0.22, 0))  # 回调记下的「播放开始」：主循环要把它打成 play: 日志
    codes, errors = [], []

    def target():
        try:
            d.run()
        except SystemExit as e:
            codes.append(e.code)
        except BaseException as e:  # noqa: BLE001  run() 里冒出来的任何异常都要看得见（v1 事故就是 AttributeError）
            errors.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=target, daemon=True)
    t.start()
    time.sleep(wait)
    alive = t.is_alive()
    if alive and quit_after:
        d._panel_control("quit")
    t.join(10 if quit_after else 0.1)  # 关流卡死的场景线程本来就不会结束
    return (codes[0] if codes else -99), d, errors, alive


root = logging.getLogger()
cap = Capture()
root.addHandler(cap)
old_level, old_exit = root.level, voice._hard_exit
root.setLevel(logging.DEBUG)
hard, done = [], threading.Event()  # 假的强制退出：记下退出码，别真把测试进程杀了
voice._hard_exit = lambda c: (hard.append(c), done.set())
voice.EXIT_DEADMAN_SEC = 600  # 平时的保险定时器不会在测试期间触发
saved = (voice.sd.Stream, voice.start_control_server, voice.InputWatchdog, voice.StreamingASR, voice.FinalASR, voice.EXIT_DEADMAN_SEC)
try:
    with tempfile.TemporaryDirectory() as dirpath:
        log = elog.init("voice", log_dir=Path(dirpath), sync=True)

        def events(*evs):
            out = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
            return [e for e in out if not evs or e["ev"] in evs]

        # 4a. 回归：有「播放开始」事件时主循环不能崩（6804b86：g 被覆盖成整数 -> AttributeError -> 退出码 1，没有任何日志）
        code, d, errors, alive = run_daemon(wait=1.6, play_event=True, stall_sec=5.0)
        check(not errors, f"run(): a play-start event in the main loop does not raise (got {errors})")
        check(alive, "run(): the daemon is still running 1.6 s after the first play event (v1 died within 0.5 s)")
        check(code == 0, f"run(): quit after a play event exits 0 (got {code})")
        check(any("play: start=" in r.getMessage() for r in cap.records), "run(): the play event is logged")
        check(d.guard.tick(False) == "ok" and d.guard.info()["frames"] > 50, "run(): guard still usable afterwards, callbacks counted")
        check(not events("audio.stall"), "run(): healthy stream -> no audio.stall event")

        # 4a'. 正常退出要撤掉保险定时器（否则 run() 返回后进程还会被它强杀，缓冲里的输出也丢了）
        voice.EXIT_DEADMAN_SEC = 0.5
        code, d, errors, alive = run_daemon(wait=1.0, stall_sec=5.0)
        time.sleep(1.2)
        check(code == 0 and hard == [], f"run(): a normal exit cancels the deadman (code={code}, forced={hard})")
        voice.EXIT_DEADMAN_SEC = 600

        # 4b. 确认停摆 -> 退出码 3 + voice.exit + WARNING + audio.stall
        cap.records.clear()
        code, d, errors, alive = run_daemon(wait=4.0, beats=False)
        check(not errors and code == voice.EXIT_RESTART and not alive,
              f"run(): confirmed callback stall -> exit code 3 by itself (code={code}, errors={errors})")
        ex = [e for e in events("voice.exit") if e["data"].get("reason") == "audio_callback_stall"]
        check(len(ex) == 1 and ex[0]["data"]["code"] == 3 and ex[0]["level"] == "error" and ex[0]["data"]["cb_age"] > 0.4,
              "run(): voice.exit(audio_callback_stall, code=3) with evidence is recorded")
        check(len(events("audio.stall")) == 1, "run(): audio.stall event recorded")
        check(any(r.levelno >= logging.WARNING and "audio_callback_stall" in r.getMessage() for r in cap.records),
              "run(): a WARNING line with the reason before exiting")
        check(FakeStream.made[0].closed, "run(): the stream was closed on the way out")

        # 4c. 关流卡死：保险定时器强制以同一退出码退出
        check(hard == [], 'run(): normal exits never trigger the deadman')
        voice.EXIT_DEADMAN_SEC = 0.6
        block = threading.Event()
        cap.records.clear()
        code, d, errors, alive = run_daemon(wait=3.0, beats=False, hang_exit=block, quit_after=False)
        check(done.wait(5) and hard == [3], f"run(): stream close hangs -> deadman forces exit with the same code 3 (got {hard})")
        check(any(e["data"].get("reason") == "exit_hang" for e in events("voice.exit")), "run(): exit_hang voice.exit event recorded")
        check(any(r.levelno >= logging.ERROR and "exit_hang" in r.getMessage() for r in cap.records), "run(): exit_hang ERROR line recorded")
        block.set()
        time.sleep(0.5)
        voice.EXIT_DEADMAN_SEC = 600

        # 4d. 游戏态：流本来就停着，没有回调是正常的，永远不判停摆
        n_stall = len(events("audio.stall"))
        code, d, errors, alive = run_daemon(wait=3.0, beats=False, paused=True)
        check(not errors and alive, f"run(): gaming state with a stopped stream is never judged a stall (alive={alive}, errors={errors})")
        check(len(events("audio.stall")) == n_stall and code == 0, f"run(): ...and quits normally (code={code})")
        log.close()
finally:
    root.removeHandler(cap)
    root.setLevel(old_level)
    voice._hard_exit = old_exit
    voice.sd.Stream, voice.start_control_server, voice.InputWatchdog, voice.StreamingASR, voice.FinalASR, voice.EXIT_DEADMAN_SEC = saved
    elog._default = elog.NullLog()

print("ALL PASS test_audio_watchdog")
