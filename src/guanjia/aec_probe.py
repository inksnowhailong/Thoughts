"""AEC 探针：验证单麦音箱条上软件回声消除能压掉多少自身回声。

流程：
1. 生成一段 4 秒的"伪语音"（多段随机频率的调幅正弦，接近人声频带）；
2. 从音箱条播放的同时用麦克风录音（同一个 sounddevice 全双工流，保证对齐）；
3. 用 pyaec（speexdsp）以播放信号为参考，对录音逐帧做回声消除；
4. 分别统计消除前/后在"播放段"的能量，给出压制量（dB）；
   另取播放结束后的 1 秒静音段作为本底噪声参照。

用法：uv run python -m guanjia.aec_probe [--in 20] [--out 16]
无需人在麦前。压制量 >= 20 dB 视为可用，>= 30 dB 视为良好。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from pyaec import Aec

SR = 16000            # 默认；运行时按设备原生采样率覆盖（WASAPI 共享模式不接受重采样）
FRAME = 160           # 10 ms
FILTER_LEN = SR // 4  # 250 ms 回声尾长，桌面音箱够用


def use_device_rate(in_dev, out_dev) -> None:
    """按输出设备的默认采样率重设 SR/FRAME/FILTER_LEN，输入输出必须同率。"""
    global SR, FRAME, FILTER_LEN
    o = sd.query_devices(out_dev if out_dev is not None else sd.default.device[1])
    i = sd.query_devices(in_dev if in_dev is not None else sd.default.device[0])
    rate = int(o["default_samplerate"])
    if int(i["default_samplerate"]) != rate:
        print(f"warn: in {i['default_samplerate']} != out {o['default_samplerate']}, using out rate")
    SR = rate
    FRAME = SR // 100
    FILTER_LEN = SR // 4
    print(f"device rate {SR}, frame {FRAME}, filter {FILTER_LEN}")


def make_probe_signal(seconds: float, sr: int) -> np.ndarray:
    """生成近似语音的测试信号：每 300ms 换一个 150~1200Hz 的调幅正弦，中间夹 100ms 停顿。"""
    rng = np.random.default_rng(7)
    out = []
    t_seg = np.arange(int(sr * 0.3)) / sr
    pause = np.zeros(int(sr * 0.1), dtype=np.float32)
    while sum(len(x) for x in out) < seconds * sr:
        f0 = rng.uniform(150, 1200)
        env = 0.5 * (1 - np.cos(2 * np.pi * t_seg / t_seg[-1]))  # 汉宁包络，避免爆音
        seg = (np.sin(2 * np.pi * f0 * t_seg) * 0.6 + np.sin(2 * np.pi * f0 * 2.02 * t_seg) * 0.25) * env
        out.append(seg.astype(np.float32))
        out.append(pause)
    sig = np.concatenate(out)[: int(seconds * sr)]
    return (sig * 0.5).astype(np.float32)


def rms_db(x: np.ndarray) -> float:
    x = x.astype(np.float64)
    return 20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9)


def run(in_dev: int | None, out_dev: int | None, seconds: float, out_dir: Path) -> dict:
    use_device_rate(in_dev, out_dev)
    play = make_probe_signal(seconds, SR)
    tail = np.zeros(SR * 1, dtype=np.float32)  # 播放后 1 秒静音，用来量本底
    play_full = np.concatenate([play, tail])
    n = len(play_full)
    rec = np.zeros(n, dtype=np.float32)
    pos = 0

    def cb(indata, outdata, frames, time_info, status):
        nonlocal pos
        if status:
            print("stream status:", status)
        end = min(pos + frames, n)
        k = end - pos
        outdata[:k, 0] = play_full[pos:end]
        if k < frames:
            outdata[k:, 0] = 0
        rec[pos:end] = indata[:k, 0]
        pos = end
        if pos >= n:
            raise sd.CallbackStop

    with sd.Stream(samplerate=SR, blocksize=FRAME, dtype="float32", channels=1,
                   device=(in_dev, out_dev), callback=cb):
        while pos < n:
            time.sleep(0.05)

    # --- 回声消除 ---
    aec = Aec(FRAME, FILTER_LEN, SR, True)
    ref_i16 = (play_full * 32767).astype(np.int16)
    mic_i16 = (np.clip(rec, -1, 1) * 32767).astype(np.int16)
    out_i16 = np.zeros_like(mic_i16)
    for i in range(0, n - FRAME + 1, FRAME):
        out_i16[i:i + FRAME] = np.asarray(
            aec.cancel_echo(mic_i16[i:i + FRAME].tolist(), ref_i16[i:i + FRAME].tolist()),
            dtype=np.int16,
        )[:FRAME]

    # 跳过前 0.5 秒（滤波器收敛期），只看播放段的稳态
    a, b = int(SR * 0.5), len(play)
    before = rms_db(mic_i16[a:b])
    after = rms_db(out_i16[a:b])
    floor = rms_db(mic_i16[len(play) + SR // 4:])  # 播放结束 250ms 后的静音本底
    result = {
        "mic_during_play_db": round(before, 1),
        "after_aec_db": round(after, 1),
        "noise_floor_db": round(floor, 1),
        "suppression_db": round(before - after, 1),
        "residual_above_floor_db": round(after - floor, 1),
        "peak_mic": round(float(np.max(np.abs(rec))), 3),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    import wave

    for name, arr in (("mic", mic_i16), ("aec", out_i16), ("ref", ref_i16)):
        with wave.open(str(out_dir / f"probe_{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(arr.tobytes())
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dev", type=int, default=None, help="输入设备号（sounddevice）")
    ap.add_argument("--out", dest="out_dev", type=int, default=None, help="输出设备号（sounddevice）")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--out-dir", type=Path, default=Path("tmp/aec_probe"))
    args = ap.parse_args()
    res = run(args.in_dev, args.out_dev, args.seconds, args.out_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
