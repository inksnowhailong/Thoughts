"""流式 ASR 封装：sherpa-onnx 流式 zipformer（中文），输入任意采样率的 float32 单声道块，输出增量文本与端点事件。

设计要点：
- 一个 `StreamingASR` 实例对应一个常驻识别器（模型只加载一次），`new_stream()` 每句话开一条流；
- `feed(stream, samples, sample_rate)` 逐块喂音频，sherpa 内部重采样到 16k；
- `is_endpoint(stream)` 由 sherpa 的端点检测判断"这句话说完了"，配合外层 VAD 使用；
- `text(stream)` 取当前累计文本；端点后调用 `reset(stream)` 开始下一句。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from guanjia.ort_preload import preload_onnxruntime

preload_onnxruntime()
import sherpa_onnx  # noqa: E402  必须在预加载之后

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30"


@dataclass
class ASRConfig:
    """识别器参数：默认 CPU 4 线程，端点检测按 sherpa 推荐值。"""

    model_dir: Path = MODEL_DIR
    num_threads: int = 4
    provider: str = "cpu"
    # 端点规则：静音 2.4s（尚无文字）/ 1.2s（已有文字）/ 单句最长 20s
    rule1_trailing_silence: float = 2.4
    rule2_trailing_silence: float = 1.2
    rule3_min_utterance_length: float = 20.0


class StreamingASR:
    """常驻的流式识别器。"""

    def __init__(self, cfg: ASRConfig | None = None) -> None:
        self.cfg = cfg or ASRConfig()
        d = self.cfg.model_dir
        self._rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(d / "tokens.txt"),
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            num_threads=self.cfg.num_threads,
            provider=self.cfg.provider,
            sample_rate=16000,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=self.cfg.rule1_trailing_silence,
            rule2_min_trailing_silence=self.cfg.rule2_trailing_silence,
            rule3_min_utterance_length=self.cfg.rule3_min_utterance_length,
            decoding_method="greedy_search",
        )

    def new_stream(self) -> sherpa_onnx.OnlineStream:
        return self._rec.create_stream()

    def feed(self, stream: sherpa_onnx.OnlineStream, samples: np.ndarray, sample_rate: int) -> None:
        """喂一块 float32 单声道音频（范围 -1~1），任意采样率。"""
        stream.accept_waveform(sample_rate, samples.astype(np.float32, copy=False))
        while self._rec.is_ready(stream):
            self._rec.decode_stream(stream)

    def finish(self, stream: sherpa_onnx.OnlineStream) -> None:
        """告知这条流没有更多音频，刷出尾部。"""
        stream.input_finished()
        while self._rec.is_ready(stream):
            self._rec.decode_stream(stream)

    def text(self, stream: sherpa_onnx.OnlineStream) -> str:
        return self._rec.get_result(stream).strip()

    def is_endpoint(self, stream: sherpa_onnx.OnlineStream) -> bool:
        return self._rec.is_endpoint(stream)

    def reset(self, stream: sherpa_onnx.OnlineStream) -> None:
        self._rec.reset(stream)


def transcribe_file(path: Path, asr: StreamingASR | None = None, chunk_ms: int = 100) -> str:
    """离线把一个 wav 按 chunk_ms 分块模拟流式喂入，返回最终文本（用于自测，不依赖麦克风）。"""
    import wave

    asr = asr or StreamingASR()
    with wave.open(str(path), "rb") as w:
        sr, n_ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    assert sw == 2, "只支持 16bit PCM"
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_ch > 1:
        pcm = pcm.reshape(-1, n_ch).mean(axis=1)
    s = asr.new_stream()
    step = sr * chunk_ms // 1000
    for i in range(0, len(pcm), step):
        asr.feed(s, pcm[i:i + step], sr)
    # 补 0.5s 静音让尾部解码完整
    asr.feed(s, np.zeros(sr // 2, dtype=np.float32), sr)
    asr.finish(s)
    return asr.text(s)


if __name__ == "__main__":
    import sys
    import time

    asr = StreamingASR()
    files = [Path(p) for p in sys.argv[1:]] or sorted((MODEL_DIR / "test_wavs").glob("*.wav"))
    for f in files:
        t0 = time.perf_counter()
        txt = transcribe_file(f, asr)
        print(f"{f.name}: {txt}   ({time.perf_counter() - t0:.2f}s)")
