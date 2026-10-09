"""声纹：sherpa-onnx 的 3D-Speaker 中文模型提取说话人向量，余弦相似度判"是不是注册过的人"。

- 模型：默认 CAM++（主人语料上与他人的区分度 0.420，优于 ERes2NetV2 的 0.384）；环境变量 GUANJIA_SPK_MODEL 可切换：
  `campplus` / `eres2netv2`，或直接给一个 .onnx 路径；
- 每个模型的嵌入空间不同、维度也可能不同，注册库按模型分文件（见 SPK_MODELS），切模型不会误用旧库；

- 注册：`enroll(name, samples)` 可多次调用叠加样本，向量存 `data/speakers.npz`；
- 放行：`identify(samples)` 返回 (name, score)，score 低于阈值返回 (None, score)；
- 阈值先取 0.5 偏松（D21），误放行由后面的"在对我说吗"判定器兜底。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from guanjia.ort_preload import preload_onnxruntime

preload_onnxruntime()
import sherpa_onnx  # noqa: E402

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
# 模型别名 → (模型文件, 该模型专用的注册库)。默认 CAM++ 用 speakers.npz；ERes2NetV2 库另存 speakers.eres2netv2.npz
SPK_MODELS = {
    "campplus": ("3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx", "speakers.npz"),
    "eres2netv2": ("3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx", "speakers.eres2netv2.npz"),
}
DEFAULT_SPK_MODEL = "campplus"
SR = 16000


def resolve_model(choice: str | None = None) -> tuple[Path, Path]:
    """按别名或 .onnx 路径解析 (模型文件, 注册库)；缺省读环境变量 GUANJIA_SPK_MODEL，再缺省用 ERes2NetV2。"""
    choice = choice or os.environ.get("GUANJIA_SPK_MODEL") or DEFAULT_SPK_MODEL
    if choice in SPK_MODELS:
        model, store = SPK_MODELS[choice]
        return _ROOT / "models" / model, _ROOT / "data" / store
    model = Path(choice)  # 自定义模型路径：注册库按模型文件名单独存
    return model, _ROOT / "data" / f"speakers.{model.stem}.npz"


SPK_MODEL, STORE = resolve_model()


class SpeakerGate:
    def __init__(self, threshold: float = 0.5, store: Path = STORE, model: Path = SPK_MODEL) -> None:
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=1, provider="cpu")
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.model = model
        self.threshold = threshold
        self.store = store
        self._db: dict[str, np.ndarray] = {}
        self._load()

    # ---- 向量 ----
    def embed(self, samples: np.ndarray, sr: int = SR) -> np.ndarray:
        s = self._ex.create_stream()
        s.accept_waveform(sr, samples.astype(np.float32, copy=False))
        s.input_finished()
        v = np.asarray(self._ex.compute(s), dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    # ---- 注册库 ----
    def _load(self) -> None:
        if self.store.exists():
            z = np.load(self.store)
            self._db = {k: z[k] for k in z.files}
            bad = [k for k, v in self._db.items() if v.shape != (self._ex.dim,)]
            if bad:  # 库和模型不配套（换过模型）：不用错库，按空库处理（守护进程会放行并告警）
                log.warning("声纹库 %s 维度与模型 %s(%d) 不符：%s，忽略该库，请重新注册", self.store, self.model.name,
                            self._ex.dim, bad)
                self._db = {}

    def _save(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        np.savez(self.store, **self._db)

    def enroll(self, name: str, samples: np.ndarray, sr: int = SR) -> None:
        """叠加一条样本到该人的均值向量。"""
        v = self.embed(samples, sr)
        if name in self._db:
            v = self._db[name] + v
            v = v / (np.linalg.norm(v) + 1e-9)
        self._db[name] = v
        self._save()

    def names(self) -> list[str]:
        return list(self._db)

    # ---- 放行 ----
    def identify(self, samples: np.ndarray, sr: int = SR) -> tuple[str | None, float]:
        if not self._db:
            return None, 0.0
        v = self.embed(samples, sr)
        best, score = None, -1.0
        for name, ref in self._db.items():
            c = float(np.dot(v, ref))
            if c > score:
                best, score = name, c
        return (best if score >= self.threshold else None), score


if __name__ == "__main__":
    # 自检：同一说话人的两段应高相似，不同说话人应低；用模型自带测试音频做粗略对照
    from guanjia.asr import MODEL_DIR
    from guanjia.vad import read_wav_16k

    g = SpeakerGate(store=Path("tmp/speakers_selftest.npz"))
    wavs = sorted((MODEL_DIR / "test_wavs").glob("*.wav"))
    embs = {w.name: g.embed(read_wav_16k(w)) for w in wavs}
    names = list(embs)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            print(f"{names[i]} vs {names[j]}: cos={float(np.dot(embs[names[i]], embs[names[j]])):.3f}")
    # 把 0.wav 的前半注册为 A，用后半识别
    a = read_wav_16k(wavs[0])
    g.enroll("A", a[: len(a) // 2])
    print("identify second half of", wavs[0].name, "->", g.identify(a[len(a) // 2:]))
    print("identify", wavs[1].name, "->", g.identify(read_wav_16k(wavs[1])))
