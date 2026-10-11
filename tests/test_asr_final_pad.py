r"""二次识别入口：模型目录选择（默认 SenseVoice 2024，环境变量一键切回 2025）、前后补缓冲、字母合并、兜底。
不加载真模型：用假识别器记录收到的音频长度。

运行：cd /d F:\coding\guanjia && .venv\Scripts\python.exe tests\test_asr_final_pad.py
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from guanjia import asr_final
from guanjia.asr_final import (MODEL_DIR_2024, MODEL_DIR_2025, PAD_SEC, SR, FinalASR, FinalASRConfig, pad_audio,
                               resolve_model_dir)


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


# ---------- 1. 模型目录 ----------
check(MODEL_DIR_2024.name == "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17", "2024 dir name")
check(MODEL_DIR_2025.name == "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", "2025 dir name kept")
check(resolve_model_dir({}) == MODEL_DIR_2024, "default -> 2024")
check(resolve_model_dir({"GUANJIA_FINAL_ASR_MODEL": ""}) == MODEL_DIR_2024, "empty env -> 2024")
check(resolve_model_dir({"GUANJIA_FINAL_ASR_MODEL": "2025"}) == MODEL_DIR_2025, "env 2025 -> old model")
check(resolve_model_dir({"GUANJIA_FINAL_ASR_MODEL": " 2024 "}) == MODEL_DIR_2024, "env 2024 (spaces ok)")
check(resolve_model_dir({"GUANJIA_FINAL_ASR_MODEL": "2031"}) == MODEL_DIR_2024, "unknown value -> 2024, no crash")
check(resolve_model_dir({"GUANJIA_FINAL_ASR_MODEL": str(Path(__file__).parent)}) == Path(__file__).parent, "existing dir path accepted")
check(asr_final.MODEL_DIR == resolve_model_dir(), "module MODEL_DIR follows env at import")
check(FinalASRConfig().model_dir == asr_final.MODEL_DIR and FinalASRConfig().language == "zh" and FinalASRConfig().use_itn,
      "config keeps zh + itn")

# ---------- 2. 补缓冲 ----------
check(PAD_SEC == 0.4, "PAD_SEC constant is 0.4")
x = np.full(SR, 0.5, dtype=np.float32)
y = pad_audio(x)
check(len(y) == SR + 2 * int(SR * PAD_SEC), "pad adds 0.4s each side")
check(y.dtype == np.float32 and not y[: int(SR * PAD_SEC)].any() and not y[-int(SR * PAD_SEC):].any(), "pad is float32 zeros")
check(np.array_equal(y[int(SR * PAD_SEC): int(SR * PAD_SEC) + SR], x), "original audio untouched in the middle")
check(len(pad_audio(x, 0.0)) == SR, "pad 0 = no-op")
check(len(pad_audio(np.zeros(0, np.float32))) == 0, "empty stays empty")


# ---------- 3. 识别入口（假识别器） ----------
class FakeRec:
    def __init__(self, text: str) -> None:
        self.text, self.seen = text, []

    def create_stream(self):
        rec = self

        class S:
            result = SimpleNamespace(text=rec.text)

            def accept_waveform(self, sr, samples):
                rec.seen.append((sr, len(samples), float(np.abs(samples[: int(SR * PAD_SEC)]).max())))

        return S()

    def decode_stream(self, s):
        pass


def make(text: str) -> FinalASR:
    f = FinalASR.__new__(FinalASR)
    f.cfg = FinalASRConfig()
    f._rec = FakeRec(text)
    f._pool = ThreadPoolExecutor(max_workers=1)
    return f


f = make("  我的 H A L O 音响声音太小了。 ")
out = f.transcribe(x, fallback="流式")
check(out == "我的 HALO 音响声音太小了。", f"transcribe merges spaced letters + strips: {out!r}")
sr_, n_, head = f._rec.seen[0]
check(sr_ == SR and n_ == SR + 2 * int(SR * PAD_SEC) and head == 0.0, "recognizer received padded audio (zeros at head)")
check(make("").transcribe(x, fallback="流式") == "流式", "empty result -> fallback")
check(make("。").transcribe(x, fallback="流式") == "流式", "punctuation-only result (2024 model on near-silence) -> fallback")
check(make(" . ").transcribe(x, fallback="") == "", "ascii punctuation-only -> empty, voice.py drops it as before")
check(make("嗯。").transcribe(x, fallback="流式") == "嗯。", "real (even filler) text kept as is")
check(make("好").transcribe(np.zeros(0, np.float32), fallback="流式") == "流式", "empty samples -> fallback, recognizer untouched")
g = make("好")
g.transcribe(np.zeros(0, np.float32), fallback="")
check(g._rec.seen == [], "no decode for empty samples")
print("ALL PASS")
