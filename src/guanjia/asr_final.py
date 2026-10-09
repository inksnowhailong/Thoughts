"""整段精识别：VAD 切出一句话后，用 sherpa-onnx 离线 SenseVoice（int8）重新识别整段，替换流式小模型的结果。

为什么要两段式：
- 流式 zipformer 小模型延迟低，适合"有人在说话"的即时反馈与打断，但字准率一般；
- SenseVoice 是非流式大模型，拿到整句上下文再识别，数字/时间/同音字明显更准，4~5s 语音 CPU 上约 0.2s；
- 实测低增益 + 噪声下 SiliconFlow 云端接口会直接返回空串，本地模型反而稳，且无网络与费用依赖。

失败兜底：识别超时、异常或结果为空时，原样返回调用方传入的流式结果，保证对话链路不断。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from guanjia.ort_preload import preload_onnxruntime

preload_onnxruntime()
import sherpa_onnx  # noqa: E402  必须在预加载之后

log = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
SR = 16000


@dataclass
class FinalASRConfig:
    """精识别参数：language 固定 zh 避免把含糊段落判成粤语/日语；use_itn=True 输出标点规整的文本。"""

    model_dir: Path = MODEL_DIR
    num_threads: int = 4
    provider: str = "cpu"
    language: str = "zh"
    use_itn: bool = True
    timeout: float = 3.0  # 单句识别超时（秒），超时回退流式结果


class FinalASR:
    """常驻的整段精识别器：模型只加载一次，识别在单独的工作线程里跑，以便施加超时。"""

    def __init__(self, cfg: FinalASRConfig | None = None) -> None:
        self.cfg = cfg or FinalASRConfig()
        d = self.cfg.model_dir
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=self.cfg.num_threads,
            provider=self.cfg.provider,
            language=self.cfg.language,
            use_itn=self.cfg.use_itn,
        )
        # 单线程执行器：识别串行进行；超时的那次仍会在后台跑完，后续请求排队
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-final")

    def _decode(self, samples: np.ndarray) -> str:
        s = self._rec.create_stream()
        s.accept_waveform(SR, samples)
        self._rec.decode_stream(s)
        return s.result.text.strip()

    def transcribe(self, samples: np.ndarray, fallback: str = "", timeout: float | None = None) -> str:
        """识别一整段语音。

        Args:
            samples: 16k 单声道 float32（-1~1），通常是 VAD Segment.samples。
            fallback: 流式小模型对同一句的结果；精识别失败/超时/为空时原样返回它。
            timeout: 本次超时秒数，缺省用配置值。

        Returns:
            精识别文本；失败时为 fallback。
        """
        if samples is None or len(samples) == 0:
            return fallback
        x = np.ascontiguousarray(samples, dtype=np.float32)
        fut = self._pool.submit(self._decode, x)
        try:
            text = fut.result(timeout=self.cfg.timeout if timeout is None else timeout)
        except FutureTimeout:
            log.warning("精识别超时（%.1fs 语音），回退流式结果", len(x) / SR)
            return fallback
        except Exception:
            log.exception("精识别失败，回退流式结果")
            return fallback
        return text or fallback


if __name__ == "__main__":
    # 自测：uv run python -m guanjia.asr_final [wav ...]，缺省跑 tmp/mic_check.wav 与 SenseVoice 自带 test_wavs
    import sys
    import time
    import wave

    def load16k(p: Path) -> np.ndarray:
        with wave.open(str(p), "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        if sr != SR:  # 自测用线性插值重采样即可
            x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * SR / sr)), np.arange(len(x)), x).astype(np.float32)
        return x

    t0 = time.perf_counter()
    asr = FinalASR()
    print(f"load {time.perf_counter() - t0:.2f}s")
    root = Path(__file__).resolve().parents[2]
    files = [Path(p) for p in sys.argv[1:]] or [root / "tmp" / "mic_check.wav", *sorted((MODEL_DIR / "test_wavs").glob("zh*.wav"))]
    for f in files:
        x = load16k(f)
        t0 = time.perf_counter()
        txt = asr.transcribe(x, fallback="<流式兜底>")
        print(f"{f.name} ({len(x) / SR:.1f}s): {txt}   ({(time.perf_counter() - t0) * 1000:.0f}ms)")
    # 兜底路径：超时与空输入都应返回 fallback
    print("timeout ->", asr.transcribe(load16k(files[0]), fallback="<流式兜底>", timeout=0.001))
    print("empty   ->", asr.transcribe(np.zeros(0, np.float32), fallback="<流式兜底>"))
