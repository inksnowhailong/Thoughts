"""放音/收音对照：先静录 1.5s 取本底，再用音箱条放 3s 的 1kHz 提示音并同时录麦。

对比两段的 RMS：放音时明显变大 = 麦和音箱都工作；不变 = 音箱没出声或麦无输入。
用法：uv run python -m guanjia.tone_test --in 20 --out 16
"""

import argparse
import json
import time

import numpy as np
import sounddevice as sd


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dev", type=int, default=20)
    ap.add_argument("--out", dest="out_dev", type=int, default=16)
    a = ap.parse_args()
    sr = int(sd.query_devices(a.out_dev)["default_samplerate"])

    quiet = sd.rec(int(sr * 1.5), samplerate=sr, channels=1, dtype="float32", device=a.in_dev)
    sd.wait()

    t = np.arange(int(sr * 3)) / sr
    tone = (0.8 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    rec = np.zeros(len(tone), dtype=np.float32)
    pos = 0

    def cb(indata, outdata, frames, time_info, status):
        nonlocal pos
        end = min(pos + frames, len(tone))
        k = end - pos
        outdata[:k, 0] = tone[pos:end]
        if k < frames:
            outdata[k:, 0] = 0
        rec[pos:end] = indata[:k, 0]
        pos = end
        if pos >= len(tone):
            raise sd.CallbackStop

    with sd.Stream(samplerate=sr, blocksize=sr // 100, dtype="float32", channels=1,
                   device=(a.in_dev, a.out_dev), callback=cb):
        while pos < len(tone):
            time.sleep(0.05)

    print(json.dumps({
        "sr": sr,
        "quiet_rms": round(rms(quiet), 6), "quiet_peak": round(float(np.max(np.abs(quiet))), 5),
        "tone_rms": round(rms(rec[sr // 2:]), 6), "tone_peak": round(float(np.max(np.abs(rec))), 5),
        "ratio_db": round(20 * np.log10((rms(rec[sr // 2:]) + 1e-9) / (rms(quiet) + 1e-9)), 1),
    }, indent=1))


if __name__ == "__main__":
    main()
