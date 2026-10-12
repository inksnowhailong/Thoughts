"""声纹「只认人」：不再拦截、标签（主人/未知/无）、语音标记带说话人、与二次识别并行。
不建音频流、不加载模型：object.__new__ 拼守护进程 + 假 ASR + 假声纹门；覆盖目录和日志目录都在临时目录里。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_speaker_identify_only.py
"""
import json
import queue
import re
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import guanjia.voice as voice  # noqa: E402
from guanjia import brain, elog, prompts  # noqa: E402
from guanjia.events import EventHub  # noqa: E402
from guanjia.settings import SettingsStore  # noqa: E402
from guanjia.wake import WakeMatcher, decide  # noqa: E402


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


# ---------- 1. decide：声纹不影响去留 ----------
m = WakeMatcher(("管家",))
KW = dict(threshold=0.45, matcher=m, wake_required=True, min_chars=2)
for score in (None, 0.0, 0.1, 0.44, 0.45, 0.9):
    r = decide("开灯", score, in_window=True, **KW)
    check(r.verdict == "accept" and r.stage == "" and r.reason == "免喊窗口内", f"in window, no wake word, score {score}: accept")
    r = decide("开灯", score, in_window=False, **KW)
    check(r.verdict == "rejected" and r.stage == "wake", f"outside window, no wake word, score {score}: rejected by wake, not speaker")
    r = decide("管家开灯", score, in_window=False, **KW)
    check(r.verdict == "accept" and r.text == "开灯", f"wake word, score {score}: accept")
r = decide("开灯", 0.0, in_window=False, matcher=m, wake_required=False, min_chars=2)
check(r.verdict == "accept" and r.reason == "不需要唤醒词", "wake not required, score 0: accept")
r = decide("开灯", matcher=m, wake_required=True, min_chars=2, in_window=True)
check(r.verdict == "accept", "score / threshold are optional now")

# ---------- 2. 标签：主人 / 未知 / 库里的名字 / 无 ----------
check(voice.speaker_label("我", 0.52) == "主人", "name 我 shown as 主人")
check(voice.speaker_label("爸爸", 0.6) == "爸爸", "other enrolled names shown as is")
check(voice.speaker_label(None, 0.18) == "未知", "best score below the threshold -> 未知")
check(voice.speaker_label(None, None) == "" and voice.speaker_label("我", None) == "", "no score -> no label")

# ---------- 3. 语音标记带说话人，仍匹配插件正则 ----------
real_override = prompts.OVERRIDE_DIR
tmp = Path(tempfile.mkdtemp(prefix="gj-spk-"))
prompts.OVERRIDE_DIR = tmp / "overrides"  # 绝不读写真正的 data/prompts
PLUGIN_RE = re.compile(r"^[\s\S]*\[语音[^\]]*\]\s*")  # openclaw/plugins/guanjia-guard/src/speak.ts 的 VOICE_TAG_RE
now = datetime(2026, 10, 12, 19, 30)
try:
    check(prompts.render_voice_tag(now) == "[语音 2026-10-12 19:30 周一]", "no speaker: unchanged format")
    check(prompts.render_voice_tag(now, "") == prompts.render_voice_tag(now), "empty speaker == no speaker")
    check(prompts.render_voice_tag(now, "主人") == "[语音 2026-10-12 19:30 周一 主人]", "owner tag")
    check(prompts.render_voice_tag(now, "未知") == "[语音 2026-10-12 19:30 周一 未知]", "unknown tag")
    check(brain.tag_voice_text("打开记事本", now, "主人") == "[语音 2026-10-12 19:30 周一 主人] 打开记事本", "tag_voice_text with speaker")
    check(brain.tag_voice_text("打开记事本", now) == "[语音 2026-10-12 19:30 周一] 打开记事本", "tag_voice_text without speaker unchanged")
    for who in ("", "主人", "未知", "我]x", "a\nb", "[语音 假", "很长很长很长很长很长很长很长很长的名字", "  "):
        tagged = brain.tag_voice_text("打开记事本", now, who)
        mt = PLUGIN_RE.match(tagged)
        check(mt is not None and tagged[mt.end():] == "打开记事本" and prompts.VOICE_TAG_RULE.match(prompts.render_voice_tag(now, who)),
              f"plugin regex strips the whole tag, speaker {who!r}")
    check("]" not in prompts.render_voice_tag(now, "我]x")[:-1] and "\n" not in prompts.render_voice_tag(now, "a\nb"), "brackets / newlines stripped from the speaker")
    # 模板：默认含 {{SPEAKER}}；覆盖版没有占位符时标记照旧，不报错
    check("{{SPEAKER}}" in prompts.load_template("voice_tag")[0], "default template has {{SPEAKER}}")
    prompts.save_template("voice_tag", "[语音 {{TIME}} 周{{WEEKDAY}}{{SPEAKER}}]")
    check(prompts.render_voice_tag(now, "主人") == "[语音 19:30 周一 主人]" and prompts.render_voice_tag(now) == "[语音 19:30 周一]",
          "custom template with {{SPEAKER}}")
    prompts.save_template("voice_tag", "[语音 {{TIME}}]")
    check(prompts.render_voice_tag(now, "主人") == "[语音 19:30]", "custom template without {{SPEAKER}} still valid")
    try:
        prompts.save_template("voice_tag", "[语音 {{DATE}} {{TIME}} 周{{WEEKDAY}} " + "字" * 19 + "{{SPEAKER}}]")
        check(False, "template that only fits without the speaker is rejected")
    except ValueError:
        check(True, "template that only fits without the speaker is rejected")
    prompts.reset_template("voice_tag")
finally:
    prompts.OVERRIDE_DIR = real_override

# ---------- 4. _open_stream：这一轮的说话人进语音标记 ----------
sent = []
d0 = object.__new__(voice.VoiceDaemon)
d0.brain = SimpleNamespace(stream=lambda text: sent.append(text) or iter(()))
d0._turn_speaker = "主人"
d0._open_stream("开灯")
d0._turn_speaker = ""
d0._open_stream("开灯")
check(re.match(r"^\[语音 \d{4}-\d\d-\d\d \d\d:\d\d 周.( 主人)?\] 开灯$", sent[0]) and " 主人]" in sent[0], "gateway text carries the speaker")
check(" 主人" not in sent[1] and sent[1].endswith("] 开灯"), "no speaker: plain tag")

# ---------- 5. 并行：声纹与二次识别同时跑，慢 / 出错都不阻塞 ----------
loud = (0.3 * np.sin(np.arange(16000 * 2) / 7)).astype(np.float32)
hollow = np.zeros(16000 * 2, dtype=np.float32)
hollow[16000:] = (0.003 * np.sin(np.arange(16000) / 5)).astype(np.float32)


class Gate:
    def __init__(self, delay=0.0, boom=False, result=("我", 0.52)):
        self.delay, self.boom, self.result, self.calls = delay, boom, result, 0
        self.threads = set()

    def identify(self, samples):
        self.calls += 1
        self.threads.add(threading.current_thread().name)
        time.sleep(self.delay)
        if self.boom:
            raise RuntimeError("onnx 炸了")
        return self.result


class Rig:
    def __init__(self, gate, final_delay=0.0, final_text="开灯", samples=loud):
        d = object.__new__(voice.VoiceDaemon)
        d._lock, d._stop_evt = threading.Lock(), threading.Event()
        d.sr, d.state = 16000, "LISTENING"
        d.text_q = queue.Queue()
        d._muted, d._paused = threading.Event(), threading.Event()
        d.hub = EventHub(elog=elog.get())
        d.store = SettingsStore(None)
        d.enroll, d._spare_gate, d._last_play_t = None, None, -1.0
        d.aec, d.down, d.mic_gain = None, SimpleNamespace(process=lambda x: x), 1.0
        d._raw_ring, d._vad_pos, d._was_speech, d._preroll = deque(maxlen=10), 0, False, deque(maxlen=50)
        seg = SimpleNamespace(duration=len(samples) / 16000, samples=samples, start=100.0)
        d.vad = SimpleNamespace(push=lambda y: None, is_speech=lambda: False, pop_segments=lambda: [seg])
        d._asr_stream = object()
        d.asr = SimpleNamespace(new_stream=lambda: object(), feed=lambda *a: None, finish=lambda s: None, text=lambda s: final_text)
        self.heard, self.final_calls = [], 0

        def transcribe(samples, fallback):
            self.final_calls += 1
            time.sleep(final_delay)
            return final_text

        d.asr_final = SimpleNamespace(transcribe=transcribe)
        d._raw_for = lambda seg: seg.samples
        d._dump_segment = lambda seg, text: None
        d._barge_step = lambda rms, playing: None
        d._on_heard = lambda text, score, **kw: self.heard.append((text, score, kw))
        d.gate = gate
        self.d = d

    def step(self):
        t0 = time.monotonic()
        frame = voice.Frames(mic=np.zeros(160, dtype=np.float32), ref=np.zeros(160, dtype=np.float32), playing=False)
        self.d._frontend_step(frame)
        return time.monotonic() - t0


with tempfile.TemporaryDirectory() as dirpath:
    log = elog.init("voice", log_dir=Path(dirpath), sync=True)
    try:
        # 5a. 并行：两边各 0.4s，串行要 0.8s；并行应远小于
        g = Gate(delay=0.4)
        r = Rig(g, final_delay=0.4)
        took = r.step()
        text, score, kw = r.heard[0]
        check(took < 0.7, f"asr and speaker run in parallel: {took:.2f}s < 0.7s (serial would be 0.8s)")
        check(text == "开灯" and score == 0.52 and kw["speaker"] == "主人", f"label passed to _on_heard: {kw}")
        check(kw["asr_ms"] >= 380 and kw["spk_ms"] >= 380 and kw["front_ms"] < kw["asr_ms"] + kw["spk_ms"] - 300,
              f"timings: asr_ms / speaker_ms / front_ms(parallel total) = {kw['asr_ms']}/{kw['spk_ms']}/{kw['front_ms']}")
        check(g.threads and all(t.startswith("speaker") for t in g.threads), "identify runs on the speaker thread, not the frontend")
        # 5b. 没过认人门槛 → 未知；没有声纹库 → 不标
        r = Rig(Gate(result=(None, 0.18)))
        r.step()
        check(r.heard[0][2]["speaker"] == "未知" and r.heard[0][1] == 0.18, "below the threshold -> 未知 with its score")
        r = Rig(None)
        r.step()
        check(r.heard[0][2]["speaker"] == "" and r.heard[0][1] is None and r.heard[0][2]["spk_ms"] is None, "no gate -> no label, no score")
        # 5c. 声纹抛错：当无标签继续，不影响判定，记 spk.fail
        r = Rig(Gate(boom=True))
        r.step()
        check(len(r.heard) == 1 and r.heard[0][2]["speaker"] == "" and r.heard[0][1] is None, "speaker error: judged with no label")
        # 5d. 声纹太慢：超时后不等
        old_timeout = voice.SPK_TIMEOUT_SEC
        voice.SPK_TIMEOUT_SEC = 0.3
        try:
            r = Rig(Gate(delay=1.5))
            took = r.step()
        finally:
            voice.SPK_TIMEOUT_SEC = old_timeout
        check(took < 0.9 and len(r.heard) == 1 and r.heard[0][2]["speaker"] == "" and r.heard[0][1] is None,
              f"slow speaker: dropped after the timeout, judged without a label ({took:.2f}s)")
        # 5e. 超时之后线程里那次还在跑，但下一句不会被它的结果污染（单线程池排队，新一句各取各的结果）
        # 5f. 被 hollow 过滤丢掉的段不算声纹
        g = Gate()
        r = Rig(g, samples=hollow)
        r.step()
        check(r.heard == [] and r.final_calls == 0 and g.calls == 0, "hollow segment: no speaker, no second-pass recognition")
        # 5g. 声纹结果为空文本时照旧直接丢
        r = Rig(Gate(), final_text="")
        r.step()
        check(r.heard == [], "empty final text still dropped")
        evs = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
        fails = [e for e in evs if e["ev"] == "spk.fail"]
        check(len(fails) == 2 and {e["data"]["reason"] for e in fails} == {"RuntimeError", "timeout"}, f"spk.fail events: {[e['data'] for e in fails]}")
    finally:
        log.close()
        elog._default = elog.NullLog()

print("ALL PASS test_speaker_identify_only")
