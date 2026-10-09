"""不出声的端到端链路测试：文字 → 网关流式 → 按句切分 → TTS 逐句合成 → 拼成一个 wav 落盘。

报告：网关首字延迟、第一句音频就绪时间（= 用户听到第一声的时间）、每句合成 RTF、总时长。
用法：uv run python -m guanjia.pipeline_test "现在几点了"
"""

import sys
import time
import wave

import numpy as np

from guanjia.brain import Brain
from guanjia.textseg import SentenceSplitter
from guanjia.tts_client import TTSClient
from guanjia.voice import SYSTEM_PROMPT


def main() -> None:
    text = " ".join(sys.argv[1:]) or "你好，简单介绍一下你自己，然后告诉我今天是星期几。"
    brain, tts, splitter = Brain(), TTSClient(), SentenceSplitter()
    t0 = time.perf_counter()
    first_token = first_audio = None
    pieces, audio, sr_out = [], [], None
    sents = []

    def say(sent: str) -> None:
        nonlocal first_audio, sr_out
        t = time.perf_counter()
        pcm, sr = tts.synth(sent)
        dt = time.perf_counter() - t
        if first_audio is None:
            first_audio = time.perf_counter() - t0
        sr_out = sr
        audio.append(pcm)
        sents.append((sent, len(pcm) / sr, dt))

    for piece in brain.stream(text, system=SYSTEM_PROMPT):
        if first_token is None:
            first_token = time.perf_counter() - t0
        pieces.append(piece)
        for s in splitter.push(piece):
            say(s)
    for s in splitter.flush():
        say(s)

    total = time.perf_counter() - t0
    print("Q:", text)
    print("A:", "".join(pieces))
    print(f"first token {first_token:.2f}s | first audio ready {first_audio:.2f}s | total {total:.2f}s")
    for s, dur, dt in sents:
        print(f"  [{dur:4.1f}s audio, synth {dt:4.1f}s, rtf {dt / max(dur, 1e-3):.2f}] {s}")
    if audio:
        pcm = np.concatenate(audio)
        with wave.open("tmp/pipeline.wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr_out)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
        print(f"wrote tmp/pipeline.wav ({len(pcm) / sr_out:.1f}s)")


if __name__ == "__main__":
    main()
