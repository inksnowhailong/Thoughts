"""识别取证对比：对 tmp/segments/ 下落盘的语音段，用不同前端处理链各跑一遍精识别（SenseVoice）与流式识别，
看是哪一步（speex AEC 预处理 / tanh 增益）损伤了语音。

处理链（都从 _raw.wav 出发，除 proc 外）：
- raw        : 原始 → FIR 降到 16k（不做 AEC、不加增益）
- raw+gain   : 原始 → 16k → tanh 增益
- aec        : 原始 → speex AEC(参考为零，只剩预处理降噪) → 16k（不加增益）
- proc       : 守护进程实际送识别的 _proc.wav（AEC 带真实参考 + 增益）

用法：uv run python -m guanjia.seg_eval [--dir tmp/segments] [--last 20] [--report tmp/seg_eval.txt]
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

from guanjia.asr import StreamingASR
from guanjia.asr_final import FinalASR
from guanjia.vad import SR, Downsampler
from guanjia.voice import MIC_GAIN, aec_cancel, apply_mic_gain, make_aec


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0, sr


def chains(raw: np.ndarray, sr: int, proc: np.ndarray) -> dict[str, np.ndarray]:
    """由原始音频构造各处理链的 16k 信号。"""
    frame = sr // 100
    n = len(raw) // frame * frame
    down = Downsampler(sr)
    raw16 = np.concatenate([down.process(raw[i:i + frame]) for i in range(0, n, frame)]) if n else raw[:0]
    aec, down2, zeros = make_aec(sr), Downsampler(sr), np.zeros(frame, np.float32)
    aec16 = np.concatenate([down2.process(aec_cancel(aec, raw[i:i + frame], zeros)) for i in range(0, n, frame)]) if n else raw[:0]
    return {"raw": raw16, "raw+gain": apply_mic_gain(raw16, MIC_GAIN), "aec": aec16, "proc": proc}


def stream_text(asr: StreamingASR, x: np.ndarray) -> str:
    s = asr.new_stream()
    asr.feed(s, x, SR)
    asr.feed(s, np.zeros(SR * 3 // 10, np.float32), SR)  # 与守护进程一致：补 0.3s 静音冲刷尾字
    asr.finish(s)
    return asr.text(s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=Path("tmp/segments"))
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--report", type=Path, default=Path("tmp/seg_eval.txt"))
    a = ap.parse_args()
    procs = sorted(a.dir.glob("*_proc.wav"))[-a.last:]
    final, streaming = FinalASR(), StreamingASR()
    lines, empty = [], {}
    for p in procs:
        raw, sr = read_wav(p.with_name(p.name.replace("_proc.wav", "_raw.wav")))
        proc, _ = read_wav(p)
        peak = float(np.max(np.abs(raw))) if len(raw) else 0.0
        lines.append(f"== {p.name}  dur={len(proc) / SR:.2f}s raw_peak={peak:.4f}")
        for name, x in chains(raw, sr, proc).items():
            f, s = final.transcribe(x, fallback=""), stream_text(streaming, x)
            empty[name] = empty.get(name, 0) + (not f)
            lines.append(f"   {name:9s} final: {f or '<空>'}   | stream: {s or '<空>'}")
    lines.append(f"# 段数 {len(procs)}；精识别空结果数 " + "  ".join(f"{k}={v}" for k, v in empty.items()))
    a.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
