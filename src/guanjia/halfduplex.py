"""半双工探针：用与守护进程相同的设备与采样率，一边放 5 秒伪语音，一边录麦克风，逐 0.5s 打印原始输入峰值。

判读：播放段的原始峰值若掉到与静音段同级甚至全零，说明设备/驱动在播放时静音了采集（硬件半双工），
这时软件 AEC 之后的打断检测不可能工作；若播放段峰值明显抬高（听到了自己的回声），则采集是全双工的。

用法：uv run python -m guanjia.halfduplex [--in N] [--out N] [--report tmp/halfduplex.txt]
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

from guanjia.aec_probe import make_probe_signal
from guanjia.voice import pick_stream_rate

PRE_SEC, PLAY_SEC, POST_SEC = 1.0, 5.0, 1.5  # 播放前静音 / 播放 / 播放后静音
WIN_SEC = 0.5  # 统计窗口


def _db(x: float) -> str:
    return f"{20 * np.log10(x + 1e-9):6.1f}"


def run(in_dev: int | None, out_dev: int | None) -> list[str]:
    """跑一次全双工收放，返回报告行。"""
    sr = pick_stream_rate(in_dev, out_dev)  # 与守护进程同一选法
    play = np.concatenate([np.zeros(int(sr * PRE_SEC), np.float32), make_probe_signal(PLAY_SEC, sr),
                           np.zeros(int(sr * POST_SEC), np.float32)])
    n, rec, pos = len(play), np.zeros(len(play), np.float32), 0

    def cb(indata, outdata, frames, time_info, status):
        nonlocal pos
        end = min(pos + frames, n)
        k = end - pos
        outdata[:, 0] = 0
        outdata[:k, 0] = play[pos:end]
        rec[pos:end] = indata[:k, 0]
        pos = end
        if pos >= n:
            raise sd.CallbackStop

    with sd.Stream(samplerate=sr, blocksize=sr // 100, dtype="float32", channels=1, latency="low",
                   device=(in_dev, out_dev), callback=cb):
        while pos < n:
            time.sleep(0.05)

    din = sd.query_devices(in_dev if in_dev is not None else sd.default.device[0])
    dout = sd.query_devices(out_dev if out_dev is not None else sd.default.device[1])
    lines = [f"# halfduplex probe {datetime.now():%Y-%m-%d %H:%M:%S}  rate={sr}",
             f"# in : {din['name']} (hostapi {din['hostapi']})",
             f"# out: {dout['name']} (hostapi {dout['hostapi']})",
             "# t(s)   phase  play_peak  raw_peak  raw_peak_dB  raw_rms_dB  zero_frac"]
    w = int(sr * WIN_SEC)
    stats = {"pre": [], "play": [], "post": []}
    for i in range(0, n - w + 1, w):
        t = i / sr
        phase = "pre" if t < PRE_SEC else ("play" if t < PRE_SEC + PLAY_SEC else "post")
        seg, ref = rec[i:i + w], play[i:i + w]
        peak, rms = float(np.max(np.abs(seg))), float(np.sqrt(np.mean(seg * seg)))
        stats[phase].append(peak)
        lines.append(f"{t:5.1f}  {phase:5s}  {np.max(np.abs(ref)):.4f}     {peak:.5f}   {_db(peak)}      {_db(rms)}     "
                     f"{np.mean(seg == 0):.2f}")
    med = {k: float(np.median(v)) if v else 0.0 for k, v in stats.items()}
    lines.append(f"# median raw_peak  pre={med['pre']:.5f}  play={med['play']:.5f}  post={med['post']:.5f}")
    quiet = max(med["pre"], med["post"])
    if med["play"] < max(quiet * 0.5, 1e-4):
        verdict = "HALF-DUPLEX: capture muted/attenuated during playback"
    elif med["play"] > quiet * 2:
        verdict = "FULL-DUPLEX: echo audible in capture during playback"
    else:
        verdict = "AMBIGUOUS: capture live but playback not picked up (check volume / output device)"
    lines.append(f"# verdict: {verdict}")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dev", type=int, default=None)
    ap.add_argument("--out", dest="out_dev", type=int, default=None)
    ap.add_argument("--report", type=Path, default=Path("tmp/halfduplex.txt"))
    a = ap.parse_args()
    lines = run(a.in_dev, a.out_dev)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
