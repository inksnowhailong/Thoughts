"""把 7ef2ffc 的纯逻辑接进守护进程：
  - _frontend_step：流式文本取完、二次识别之前，噪声门掏空的假触发段（is_hollow_segment）直接丢弃，不进二次识别和判定；
    二次识别之后，流式没出字而最终出了「像一句话的中文」（is_unconfirmed_by_stream）当成没识别出字，直接丢；
    每次丢弃记一条 seg.drop 事件（why=hollow/unconfirmed、段长、两份文本）；注册模式下的段先被注册拿走，不经过这两个过滤。
  - _on_heard：decide(..., alt_text=流式文本 or None)，二次识别漏掉唤醒词时能靠流式结果救回。
不建音频流、不加载模型：object.__new__ 拼守护进程 + 假 ASR。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_asr_wire.py
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
hollow = np.zeros(16000 * 2, dtype=np.float32)  # 一半是精确的 0（噪声门），另一半是极弱底噪
hollow[16000:] = (0.003 * np.sin(np.arange(16000) / 5)).astype(np.float32)


class Rig:
    """半个守护进程 + 假 ASR：每次 step 吐出给定的一段，记录谁被调用了。"""

    def __init__(self, stream_text, final_text, samples):
        d = object.__new__(voice.VoiceDaemon)
        d._lock, d._stop_evt = threading.Lock(), threading.Event()
        d.sr = 16000
        d.state = "LISTENING"
        d.text_q = queue.Queue()
        d._muted, d._paused = threading.Event(), threading.Event()
        d._play_buf, d._play_pos, d.play_q = np.zeros(0, dtype=np.float32), 0, queue.Queue()
        d.hub = EventHub(elog=elog.get())
        d.store = SettingsStore(None)
        d.enroll, d._spare_gate, d._last_play_t = None, None, -1.0
        d.aec, d.down, d.mic_gain = None, SimpleNamespace(process=lambda x: x), 1.0
        d._raw_ring, d._vad_pos, d._was_speech, d._preroll = deque(maxlen=10), 0, False, deque(maxlen=50)
        self.seg = SimpleNamespace(duration=len(samples) / 16000, samples=samples, start=100.0)
        d.vad = SimpleNamespace(push=lambda y: None, is_speech=lambda: False, pop_segments=lambda: [self.seg])
        d._asr_stream = object()
        self.final_calls, self.heard, self.dumped = [], [], []
        d.asr = SimpleNamespace(new_stream=lambda: object(), feed=lambda *a: None, finish=lambda s: None, text=lambda s: stream_text)

        def transcribe(samples, fallback):
            self.final_calls.append(fallback)
            return final_text

        d.asr_final = SimpleNamespace(transcribe=transcribe)
        d._raw_for = lambda seg: seg.samples  # 取证用的原始帧：测试里直接用同一份
        d._dump_segment = lambda seg, text: self.dumped.append(text)
        d._barge_step = lambda rms, playing: None
        d._on_heard = lambda text, score, **kw: self.heard.append((text, kw))
        d.gate = None
        self.d = d

    def step(self):
        frame = voice.Frames(mic=np.zeros(160, dtype=np.float32), ref=np.zeros(160, dtype=np.float32), playing=False)
        self.d._frontend_step(frame)


with tempfile.TemporaryDirectory() as dirpath:
    log = elog.init("voice", log_dir=Path(dirpath), sync=True)
    try:
        # 1. 掏空的假触发：不进二次识别、不进判定，记 seg.drop
        r = Rig("", "我", hollow)
        r.step()
        check(r.final_calls == [] and r.heard == [], "hollow segment: no second-pass recognition, no judging")
        drops = events(dirpath, "seg.drop")
        check(len(drops) == 1 and drops[0]["data"]["why"] == "hollow" and drops[0]["data"]["sec"] == 2.0
              and drops[0]["data"]["zero_frac"] >= 0.3, f"seg.drop why=hollow with duration and zero fraction: {drops[0]['data']}")
        check(r.d.hub.live["asr"]["state"] == "done" and "假触发" in r.d.hub.live["asr"]["note"], "panel live status says why it was dropped")
        check(r.dumped == ["hollow"], "dropped segment is still dumped for forensics (labelled hollow)")

        # 2. 正常段：照常识别，把流式文本也带给 _on_heard
        n = len(events(dirpath, "seg.drop"))
        r = Rig("开灯", "开灯", loud)
        r.step()
        check(r.final_calls == ["开灯"] and len(r.heard) == 1 and r.heard[0][0] == "开灯" and r.heard[0][1]["stream_text"] == "开灯"
              and len(events(dirpath, "seg.drop")) == n, "normal segment goes through untouched")

        # 3. 流式没出字、最终出了中文：当幻觉直接丢
        r = Rig("", "走走了吧", loud)
        r.step()
        d3 = events(dirpath, "seg.drop")[-1]
        check(r.final_calls == [""] and r.heard == [], "unconfirmed text is dropped before judging")
        check(d3["data"]["why"] == "unconfirmed" and d3["data"]["text"] == "走走了吧" and d3["data"]["stream_text"] == ""
              and d3["data"]["sec"] == 2.0, f"seg.drop why=unconfirmed carries both texts: {d3['data']}")
        check(r.d.hub.live["asr"]["state"] == "done" and "没识别出字" in r.d.hub.live["asr"]["note"], "treated like 'no text recognised' on the panel")
        check(r.dumped == ["走走了吧"] or r.dumped == ["unconfirmed"], "segment dumped for forensics")

        # 4. 不该误丢的
        n = len(events(dirpath, "seg.drop"))
        for stream, final, what in (("管家开灯", "管家开灯", "both texts agree"), ("开灯", "打开灯", "stream has text (differs)"),
                                    ("", "OK", "pure English with empty stream"), ("", "好", "single char below min_chars")):
            r = Rig(stream, final, loud)
            r.step()
            check(len(r.heard) == 1 and r.heard[0][0] == final, f"not dropped: {what}")
        check(len(events(dirpath, "seg.drop")) == n, "no seg.drop for those")

        # 5. 精识别本来就空：原有路径（不记 seg.drop）
        r = Rig("", "", loud)
        r.step()
        check(r.heard == [] and r.d.hub.live["asr"]["note"] == "没识别出字" and len(events(dirpath, "seg.drop")) == n, "empty final text keeps the old path")

        # 6. 注册模式：段先被注册拿走，掏空的段也不经过这两个过滤（由注册自己的质量检查拒收）
        r = Rig("", "我", hollow)
        taken = []
        r.d.enroll = SimpleNamespace(expired=lambda: False, state="recording", samples=[], last=None,
                                     feed=lambda samples, playing=False: taken.append(len(samples)) or {"ok": False, "reason": "声音太小", "group": "near", "total": 0})
        r.step()
        check(taken == [len(hollow)] and r.final_calls == [] and r.heard == [] and len(events(dirpath, "seg.drop")) == n,
              "enroll mode: segment consumed by enrollment, no seg.drop, no recognition")

        # 7. decide 带 alt_text
        seen = []
        real_decide = voice.decide

        def spy(text, score=None, **kw):
            seen.append(kw.get("alt_text", "MISSING"))
            return real_decide(text, score, **kw)

        voice.decide = spy
        try:
            d = object.__new__(voice.VoiceDaemon)
            d._lock = threading.Lock()
            d.state, d.gate_mode = "LISTENING", "enforce"
            d.text_q, d.play_q = queue.Queue(), queue.Queue()
            d._earcon = np.ones(4, dtype=np.float32)
            d._gen, d.sr = 0, 16000
            d.hub = EventHub(elog=log)
            d.store = SettingsStore(None)
            d.wake = WakeMatcher(("管家",))
            d._followup_until = 0.0
            d._turn_is_say = False
            d._ack_pcm = None
            d.status = lambda: {}
            d._wake_ack = lambda uid: None
            d._on_heard("打开音响", 0.6, source="mic", stream_text="管家打开音响")
            d._on_heard("打开音响", 0.6, source="mic", stream_text="")
            d._on_heard("打开音响", None, source="inject")
        finally:
            voice.decide = real_decide
        check(seen == ["管家打开音响", None, None], f"decide gets alt_text=stream_text or None: {seen}")
    finally:
        log.close()
        elog._default = elog.NullLog()

print("ALL PASS test_asr_wire")
