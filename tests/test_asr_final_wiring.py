r"""voice.py 与二次识别后端的接线：transcribe_ex 的结果（后端名/耗时/置信度）进记录与 asr.done 事件；
asr.fallback / asr.backend 事件接到 elog；游戏态调用 enter_gaming / leave_gaming。
用 object.__new__ 拼半个守护进程 + 假识别器，不加载模型。

运行：cd /d F:\coding\guanjia && .venv\Scripts\python.exe tests\test_asr_final_wiring.py
"""
import json
import queue
import tempfile
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia import elog
from guanjia.asr_final import FinalResult
from guanjia.events import EventHub
from guanjia.settings import SettingsStore
from guanjia.wake import WakeMatcher


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def events(dirpath, *evs):
    out = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
    return [e for e in out if not evs or e["ev"] in evs]


loud = (0.3 * np.sin(np.arange(16000 * 2) / 7)).astype(np.float32)

with tempfile.TemporaryDirectory() as dirpath:
    log = elog.init("voice", log_dir=Path(dirpath), sync=True)
    try:
        # ---------- 1. _frontend_step 把 FinalResult 原样交给 _on_heard ----------
        res = FinalResult("管家开灯", "fireredasr2", 180, 0.97, 0.62)
        d = object.__new__(voice.VoiceDaemon)
        d._lock, d._stop_evt = threading.Lock(), threading.Event()
        d.sr, d.state = 16000, "LISTENING"
        d.text_q = queue.Queue()
        d._muted, d._paused = threading.Event(), threading.Event()
        d._play_buf, d._play_pos, d.play_q = np.zeros(0, dtype=np.float32), 0, queue.Queue()
        d.hub = EventHub(elog=elog.get())
        d.store = SettingsStore(None)
        d.enroll, d._spare_gate, d._last_play_t = None, None, -1.0
        d.aec, d.down, d.mic_gain = None, SimpleNamespace(process=lambda x: x), 1.0
        d._raw_ring, d._vad_pos, d._was_speech, d._preroll = deque(maxlen=10), 0, False, deque(maxlen=50)
        seg = SimpleNamespace(duration=2.0, samples=loud, start=100.0)
        d.vad = SimpleNamespace(push=lambda y: None, is_speech=lambda: False, pop_segments=lambda: [seg])
        d._asr_stream = object()
        d.asr = SimpleNamespace(new_stream=lambda: object(), feed=lambda *a: None, finish=lambda s: None, text=lambda s: "管家开灯")
        d.asr_final = SimpleNamespace(transcribe_ex=lambda samples, fallback: res)
        d._raw_for = lambda s: s.samples
        d._dump_segment = lambda s, text: None
        d._barge_step = lambda rms, playing: None
        d.gate = None
        heard = []
        d._on_heard = lambda text, score, **kw: heard.append((text, kw))
        d._frontend_step(voice.Frames(mic=np.zeros(160, dtype=np.float32), ref=np.zeros(160, dtype=np.float32), playing=False))
        check(len(heard) == 1 and heard[0][0] == "管家开灯" and heard[0][1]["asr"] is res, "_on_heard gets the FinalResult as asr=")

        # ---------- 1b. 起判沿（有人开口）→ 让 GPU 服务给显卡加热，每次开口只一次 ----------
        warms = []
        d.asr_final = SimpleNamespace(transcribe_ex=lambda samples, fallback: res, prewarm=lambda: warms.append(1))
        d.vad = SimpleNamespace(push=lambda y: None, is_speech=lambda: True, pop_segments=lambda: [])
        d._was_speech = False
        frame = voice.Frames(mic=np.zeros(160, dtype=np.float32), ref=np.zeros(160, dtype=np.float32), playing=False)
        d._frontend_step(frame)
        d._frontend_step(frame)
        check(warms == [1], f"prewarm fires once on the speech-onset edge, not on every frame: {warms}")
        d._was_speech = False
        d.asr_final = SimpleNamespace(transcribe_ex=lambda samples, fallback: res)  # 老式假识别器没有 prewarm：不能报错
        d._frontend_step(frame)
        d.asr_final = None
        d._was_speech = False
        d._frontend_step(frame)
        check(True, "no prewarm attribute / no asr_final: frontend keeps running")

        # ---------- 2. 真 _on_heard：后端名/耗时/置信度进记录和 asr.done 事件 ----------
        d2 = object.__new__(voice.VoiceDaemon)
        d2._lock = threading.Lock()
        d2.state, d2.gate_mode = "LISTENING", "identify-only"
        d2.text_q, d2.play_q = queue.Queue(), queue.Queue()
        d2._earcon = np.ones(4, dtype=np.float32)
        d2._gen, d2.sr = 0, 16000
        d2.hub = EventHub(elog=log)
        d2.store = SettingsStore(None)
        d2.wake = WakeMatcher(("管家",))
        d2._followup_until = 0.0
        d2._turn_is_say = False
        d2._ack_pcm = None
        d2.status = lambda: {}
        d2._wake_ack = lambda uid: None
        out = d2._on_heard("管家开灯", 0.6, source="mic", stream_text="管家开灯", asr_ms=180, asr=res)
        rec = d2.hub.record(out["uid"])
        check(rec["asr_backend"] == "fireredasr2" and rec["asr_conf"] == 0.97 and rec["asr_min_conf"] == 0.62 and rec["asr_fallback"] is None,
              f"record carries backend / confidence: {({k: v for k, v in rec.items() if k.startswith('asr_')})}")
        ev = [e for e in events(dirpath, "asr.done") if (e.get("uid") or e["data"].get("uid")) == out["uid"]]
        check(len(ev) == 1 and ev[0]["data"]["backend"] == "fireredasr2" and ev[0]["data"]["ms"] == 180 and ev[0]["data"]["conf"] == 0.97
              and ev[0]["data"]["min_conf"] == 0.62, f"asr.done event has backend/ms/conf: {ev[0]['data'] if ev else None}")
        fb = FinalResult("管家开灯", "sensevoice", 240, fallback="timeout")
        out = d2._on_heard("管家开灯", 0.6, source="mic", stream_text="管家开灯", asr=fb)
        rec = d2.hub.record(out["uid"])
        check(rec["asr_backend"] == "sensevoice" and rec["asr_fallback"] == "timeout" and rec["asr_conf"] is None, "fallback segment: backend sensevoice + reason")
        out = d2._on_heard("管家开灯", None, source="inject", dry_run=True)
        check("asr_backend" not in d2.hub.record(out["uid"]), "inject path (no asr info) unchanged")

        # ---------- 3. _asr_event → elog ----------
        d2._asr_event("asr.fallback", "FireRedASR2 timeout，这一段改用 SenseVoice", level="warn", reason="timeout", fails=1)
        e = events(dirpath, "asr.fallback")[-1]
        check(e["comp"] == "asr" and e["level"] == "warn" and e["data"]["reason"] == "timeout" and e["data"]["fails"] == 1, f"asr.fallback event: {e}")
        d2._asr_event("asr.backend", "恢复", state="recovered")
        e = events(dirpath, "asr.backend")[-1]
        check(e["level"] == "info" and e["data"]["state"] == "recovered", "asr.backend event (info)")

        # ---------- 4. 游戏态 ----------
        calls = []
        final = SimpleNamespace(enter_gaming=lambda: calls.append("enter"), leave_gaming=lambda: calls.append("leave"))
        d3 = object.__new__(voice.VoiceDaemon)
        d3.watchdog = None
        d3.in_q, d3.text_q = queue.Queue(), queue.Queue()
        d3.asr, d3.asr_final = object(), final
        d3.tts = SimpleNamespace(control=lambda action: None)
        stream = SimpleNamespace(stop=lambda: None, start=lambda: None)
        d3._suspend_audio(stream)
        check(calls == ["enter"] and d3.asr is None and d3.asr_final is final, "entering gaming: streaming ASR released, final ASR kept and told to watch VRAM")
        saved = voice.StreamingASR
        voice.StreamingASR = lambda: "stream-asr"
        d3._reset_frontend = lambda: None
        try:
            d3._resume_audio(stream)
        finally:
            voice.StreamingASR = saved
        check(calls == ["enter", "leave"] and d3.asr == "stream-asr" and d3.asr_final is final, "leaving gaming: final ASR told to reload, same object")
        d4 = object.__new__(voice.VoiceDaemon)  # 老用例拼的半个守护进程没有 asr_final：不能报错
        d4.watchdog, d4.in_q, d4.text_q, d4.asr = None, queue.Queue(), queue.Queue(), object()
        d4.tts = SimpleNamespace(control=lambda action: None)
        d4._suspend_audio(stream)
        check(True, "no asr_final attribute: suspend does not raise")
    finally:
        log.close()
        elog._default = elog.NullLog()

print("ALL PASS test_asr_final_wiring")

