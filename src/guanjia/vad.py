"""语音活动检测（VAD）：sherpa-onnx 的 Silero VAD，输入 16k float32 块，输出"有人声/无人声"与切出的整段语音。

两种用法：
- 门控：`is_speech(chunk)` 只回答当前这块有没有人声（打断检测用，延迟一块 32ms）；
- 分段：`push(chunk)` 累积，`pop_segments()` 取出已经说完的整段音频（给声纹用，声纹要整句才准）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from guanjia.ort_preload import preload_onnxruntime

preload_onnxruntime()
import sherpa_onnx  # noqa: E402

VAD_MODEL = Path(__file__).resolve().parents[2] / "models" / "silero_vad.onnx"
SR = 16000
WINDOW = 512  # Silero 在 16k 下固定 512 采样（32ms）


@dataclass
class Segment:
    """一段完整语音。start 秒、samples 为 16k float32。"""

    start: float
    samples: np.ndarray

    @property
    def duration(self) -> float:
        return len(self.samples) / SR


class VAD:
    def __init__(self, threshold: float = 0.5, min_silence: float = 0.8, min_speech: float = 0.25) -> None:
        """min_silence 取 0.8s：一句话中间自然停顿 0.4~0.6s 不会被切成两段。"""
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(VAD_MODEL)
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = min_speech
        cfg.silero_vad.window_size = WINDOW
        cfg.sample_rate = SR
        cfg.num_threads = 1
        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
        self._pending = np.zeros(0, dtype=np.float32)

    def push(self, chunk: np.ndarray) -> None:
        """喂 16k float32 任意长度块；内部按 512 采样切给模型。"""
        buf = np.concatenate([self._pending, chunk.astype(np.float32, copy=False)])
        n = (len(buf) // WINDOW) * WINDOW
        for i in range(0, n, WINDOW):
            self._vad.accept_waveform(buf[i:i + WINDOW])
        self._pending = buf[n:]

    def is_speech(self) -> bool:
        """当前是否处于有人声状态（含正在说的一段）。"""
        return self._vad.is_speech_detected()

    def pop_segments(self) -> list[Segment]:
        """取出已结束的语音段。"""
        out: list[Segment] = []
        while not self._vad.empty():
            seg = self._vad.front
            out.append(Segment(start=seg.start / SR, samples=np.asarray(seg.samples, dtype=np.float32)))
            self._vad.pop()
        return out

    def flush(self) -> list[Segment]:
        self._vad.flush()
        return self.pop_segments()

    def reset(self) -> None:
        self._vad.reset()
        self._pending = np.zeros(0, dtype=np.float32)


def _fir_lowpass(cutoff: float, taps: int = 31) -> np.ndarray:
    """窗函数法 FIR 低通（cutoff 为输入奈奎斯特的比例），用于整数倍降采样前的抗混叠。"""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


class Downsampler:
    """设备采样率（16k 的整数倍）→ 16k 的流式降采样器：FIR 抗混叠 + 抽取，跨块保留滤波器尾巴。

    守护进程与声纹注册共用这一条链，保证注册向量和运行时向量来自同一信号域。
    """

    def __init__(self, sr: int) -> None:
        assert sr % SR == 0, f"采样率 {sr} 不是 16k 的整数倍"
        self.decim = sr // SR
        self._fir = _fir_lowpass(0.9 / self.decim)  # 截止频率随抽取倍数走，32k/96k 也不混叠
        self._tail = np.zeros(len(self._fir) - 1, dtype=np.float32)
        self._phase = 0  # 下一块从第几个样本开始抽取，保证块长不是 decim 整数倍时相位也连续

    def process(self, x: np.ndarray) -> np.ndarray:
        """喂一块设备采样率的 float32，返回对应的 16k 样本。"""
        if self.decim == 1:
            return x.astype(np.float32, copy=False)
        buf = np.concatenate([self._tail, x.astype(np.float32, copy=False)])
        y = np.convolve(buf, self._fir, mode="valid")
        out = y[self._phase:: self.decim]
        self._phase = (self._phase - len(y)) % self.decim
        self._tail = buf[-(len(self._fir) - 1):]
        return out


def read_wav_16k(path: Path) -> np.ndarray:
    """读 wav 为 16k 单声道 float32（简单线性重采样，仅测试用）。"""
    import wave

    with wave.open(str(path), "rb") as w:
        sr, n_ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch > 1:
        pcm = pcm.reshape(-1, n_ch).mean(axis=1)
    if sr != SR:
        x_old = np.linspace(0, 1, len(pcm), endpoint=False)
        x_new = np.linspace(0, 1, int(len(pcm) * SR / sr), endpoint=False)
        pcm = np.interp(x_new, x_old, pcm).astype(np.float32)
    return pcm


if __name__ == "__main__":
    import sys

    from guanjia.asr import MODEL_DIR

    files = [Path(p) for p in sys.argv[1:]] or sorted((MODEL_DIR / "test_wavs").glob("*.wav"))
    vad = VAD()
    for f in files:
        pcm = read_wav_16k(f)
        vad.reset()
        for i in range(0, len(pcm), 1600):
            vad.push(pcm[i:i + 1600])
        segs = vad.flush()
        print(f"{f.name}: {len(pcm) / SR:.1f}s -> {len(segs)} segments " + ", ".join(f"[{s.start:.2f}s +{s.duration:.2f}s]" for s in segs))
