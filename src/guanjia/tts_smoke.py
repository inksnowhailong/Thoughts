"""TTS 服务冒烟：合成两句，记录耗时与音频时长，落到 tmp/（不放声）。"""

import time
import wave

import numpy as np

from guanjia.tts_client import TTSClient

c = TTSClient()
print("alive:", c.alive())
for i, text in enumerate(["你好，我是家里的管家，今天想让我做点什么？", "第二句用来看速度，现在几点了。"]):
    t0 = time.perf_counter()
    pcm, sr = c.synth(text)
    dt = time.perf_counter() - t0
    print(f"[{i}] {len(pcm) / sr:.2f}s audio @ {sr}Hz, synth {dt:.2f}s, rtf {dt / (len(pcm) / sr):.2f}, peak {float(np.max(np.abs(pcm))):.2f}")
    with wave.open(f"tmp/tts_{i}.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
