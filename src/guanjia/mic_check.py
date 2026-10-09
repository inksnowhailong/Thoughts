"""麦克风电平检查：倒数后录 5 秒（系统默认输入设备、原生采样率），打印峰值/RMS（dBFS），结果写 tmp/mic_check.txt，音频存 tmp/mic_check.wav。

用于判断当前麦克风在正常说话距离下的信号强度，决定守护进程要不要加软件增益。
"""

import time
import wave

import numpy as np
import sounddevice as sd


def db(x: float) -> float:
    return 20 * np.log10(max(x, 1e-9))


def main() -> None:
    dev = sd.query_devices(sd.default.device[0])
    sr = int(dev["default_samplerate"])
    print(f"输入设备：{dev['name']} @ {sr}Hz")
    input("准备好后按【回车】开始录音（录 5 秒，用平时说话的音量对着麦克风说）...")
    print("正在录音 >>> 请说话", flush=True)
    rec = sd.rec(int(sr * 5), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    x = rec[:, 0]
    peak, rms = float(np.max(np.abs(x))), float(np.sqrt(np.mean(x * x)))
    # 按 0.5s 分块看最响的一段（更接近语音峰值）
    blk = sr // 2
    rms_blocks = [float(np.sqrt(np.mean(x[i:i + blk] ** 2))) for i in range(0, len(x) - blk, blk)]
    loud = max(rms_blocks) if rms_blocks else rms
    gain = 0.1 / max(loud, 1e-9)
    lines = [
        f"device={dev['name']} sr={sr}",
        f"peak_dbfs={db(peak):.1f} rms_dbfs={db(rms):.1f} loud_rms_dbfs={db(loud):.1f}",
        f"suggested_gain=x{min(gain, 100):.0f}",
    ]
    text = "\n".join(lines)
    print(text)
    with open("tmp/mic_check.txt", "w", encoding="utf-8") as f:  # 结果落文件，远端读取
        f.write(text + "\n")
    with wave.open("tmp/mic_check.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    print("已存 tmp/mic_check.wav")
    input("按【回车】关闭窗口...")


if __name__ == "__main__":
    main()
