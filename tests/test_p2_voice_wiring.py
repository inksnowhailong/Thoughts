"""P2 守护进程接线用例：不建音频流、不加载模型，用 object.__new__ 拼一个最小守护进程，验证
say / stop_speaking（不关网关流）/ 新一句先停播再放提示音 / 一轮结束复位 / 托盘状态 / 退出码。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p2_voice_wiring.py
"""
import inspect
import queue
import threading
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia.status import Health
from guanjia.turns import SayItem, TurnResult


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


check((voice.EXIT_QUIT, voice.EXIT_CRASH, voice.EXIT_RESTART) == (0, 1, 3), "exit codes match the supervisor")
src = inspect.getsource(voice)
check("guanjia.front" not in src and "guanjia.backoffice" not in src, "front/backoffice out of the call chain")
check("brain.abort" not in src, "barge-in never closes the gateway stream")


class FakeTTS:
    def __init__(self) -> None:
        self.aborts = 0

    def abort(self) -> None:
        self.aborts += 1


class NoAbortBrain:
    def abort(self) -> None:
        raise AssertionError("stop_speaking must not close the gateway stream")


class FakeRunner:
    def __init__(self) -> None:
        self.got = None

    def run_reply(self, text, gen):
        self.got = (text, gen)
        return TurnResult(reply="好的。", spoken=["好的。"])

    def run_say(self, text, gen):
        return TurnResult(reply=text, unspoken=[text], error="TTS：ConnectError: refused")


d = object.__new__(voice.VoiceDaemon)
d._lock, d._stop_evt = threading.Lock(), threading.Event()
d._gen, d.state = 0, "LISTENING"
d.play_q, d.text_q = queue.Queue(), queue.Queue()
d._muted, d._paused = threading.Event(), threading.Event()
d._play_buf, d._play_pos = np.zeros(0, dtype=np.float32), 0
d._earcon = np.ones(4, dtype=np.float32)
d._turn_first, d._turn_t0 = False, 0.0
d.tray, d.last_error, d.sleep_state = None, "", "awake"
d.tts, d.brain, d.runner = FakeTTS(), NoAbortBrain(), FakeRunner()
d.health = Health(True, True)
d.runtime = SimpleNamespace(state="ACTIVE")
d._exit_code = None

check(d.say("你好") == (True, "queued"), "say queued")
item = d.text_q.get_nowait()
check(isinstance(item, SayItem) and item.text == "你好", "say puts a SayItem on the brain queue")
d._muted.set()
check(d.say("x") == (False, "muted"), "say refused while muted")
d._muted.clear()
d._paused.set()
check(d.say("x") == (False, "gaming"), "say refused while gaming")
d._paused.clear()

d.state = "SPEAKING"
d.play_q.put((0, np.zeros(3, dtype=np.float32)))
d.stop_speaking("barge-in")
check(d._gen == 1 and d.state == "LISTENING" and d.play_q.empty(), "stop: new generation, queue cleared, listening")
check(d.tts.aborts == 1, "stop only aborts the TTS request")

d.state = "SPEAKING"
d._accept_utterance("打开记事本")
check(d._gen == 2, "new utterance while speaking stops playback first")
g, pcm = d.play_q.get_nowait()
check(g == 2 and len(pcm) == 4, "earcon queued immediately with the new generation")
check(d.text_q.get_nowait() == "打开记事本", "utterance queued for the brain")

d._handle_item("打开记事本")
check(d.runner.got == ("打开记事本", 2) and d.state == "LISTENING", "turn handed to TurnRunner, back to listening")
d._handle_item(SayItem("念这句"))
check(d.last_error.startswith("TTS"), "unspoken say records an error (tray turns red)")
d._handle_item("再来一句")
check(d.last_error == "", "next successful turn clears the error")

s = d.status()
check(s["view"] == "listening" and s["gateway"] is True and s["tts"] is True and s["sleep"] == "awake", "status fields")
d.toggle_mute()
check(d._muted.is_set() and d.status()["view"] == "muted", "tray mute")
d.toggle_mute()
check(not d._muted.is_set() and d.status()["view"] == "listening", "tray unmute")
check(d.set_sleep_state("sleeping") and d.status()["sleep"] == "sleeping", "P4 sleep bit")

d.request_exit(voice.EXIT_RESTART)
check(d._exit_code == 3 and d._stop_evt.is_set(), "tray restart -> exit code 3")
print("ALL PASS test_p2_voice_wiring")
