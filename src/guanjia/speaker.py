"""声纹：sherpa-onnx 的 3D-Speaker 中文模型提取说话人向量，余弦相似度判"是谁"。

- 模型：默认 CAM++（`campplus`）；环境变量 GUANJIA_SPK_MODEL 可切 `eres2netv2` 或给 .onnx 路径；库按模型分文件（SPK_MODELS）。
- 库格式 v2：每个人存全部注册样本的向量 [N, dim]；打分 = 对该人样本余弦的 top-3 平均（N<3 时取全部）。
  旧格式（每人一个向量）照常读，第一次保存时整库改写成 v2。
- 门槛默认取 settings.Settings().spk_threshold；软门禁后只在免喊窗口里当门槛，其余只作身份标签。
- 注册：面板引导（enroll_session.py，守护进程同一条链路）或命令行 enroll.py；保存前备份，可回滚。
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

import numpy as np

from guanjia.ort_preload import preload_onnxruntime
from guanjia.settings import Settings

preload_onnxruntime()
import sherpa_onnx  # noqa: E402

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
# 模型别名 → (模型文件, 该模型专用的注册库)：默认 CAM++ 用 speakers.npz，ERes2NetV2 单独用 speakers.eres2netv2.npz
SPK_MODELS = {
    "campplus": ("3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx", "speakers.npz"),
    "eres2netv2": ("3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx", "speakers.eres2netv2.npz"),
}
DEFAULT_SPK_MODEL = "campplus"
SR = 16000
TOP_K = 3
STORE_VERSION = 2
DEFAULT_THRESHOLD = Settings().spk_threshold


def resolve_model(choice: str | None = None) -> tuple[Path, Path]:
    """别名或 .onnx 路径 → (模型文件, 注册库)。缺省看环境变量 GUANJIA_SPK_MODEL，再缺省用 CAM++。"""
    choice = choice or os.environ.get("GUANJIA_SPK_MODEL") or DEFAULT_SPK_MODEL
    if choice in SPK_MODELS:
        model, store = SPK_MODELS[choice]
        return _ROOT / "models" / model, _ROOT / "data" / store
    model = Path(choice)  # 自定义模型路径：注册库按模型文件名单独存
    return model, _ROOT / "data" / f"speakers.{model.stem}.npz"


SPK_MODEL, STORE = resolve_model()


def backup_path(store: Path) -> Path:
    """重新注册前的旧库备份：只建一次，回滚用。"""
    name = "speakers.campp.pre-reenroll.npz" if store.name == "speakers.npz" else f"{store.stem}.pre-reenroll.npz"
    return store.with_name(name)


def score_against(v: np.ndarray, refs: np.ndarray, k: int = TOP_K) -> float:
    """v 与一个人全部样本的余弦，取最大的 k 个求平均。"""
    sims = refs @ v
    k = min(k, len(sims))
    return float(np.sort(sims)[-k:].mean())


def load_store(path: Path, dim: int) -> tuple[dict[str, np.ndarray], bool]:
    """读库：返回 ({名字: [N, dim]}, 是否旧格式)。维度不符抛 ValueError。"""
    with np.load(path, allow_pickle=False) as z:
        files = set(z.files)
        if "version" in files:
            names = [str(n) for n in z["names"]]
            embs, owner = z["embs"].astype(np.float32), z["owner"]
            db = {n: embs[owner == i] for i, n in enumerate(names)}
            legacy = False
        else:
            db = {k: np.asarray(z[k], dtype=np.float32).reshape(1, -1) for k in files}
            legacy = True
    bad = [k for k, v in db.items() if v.ndim != 2 or v.shape[1] != dim or len(v) == 0]
    if bad:
        raise ValueError(f"声纹库 {path.name} 与模型维度 {dim} 不符：{bad}")
    return db, legacy


def save_store(path: Path, db: dict[str, np.ndarray]) -> None:
    """整库写成 v2：同目录临时文件 + os.replace。"""
    names = list(db)
    dim = next(iter(db.values())).shape[1] if db else 0
    embs = np.concatenate([db[n] for n in names]).astype(np.float32) if names else np.zeros((0, dim), np.float32)
    owner = np.concatenate([np.full(len(db[n]), i, np.int32) for i, n in enumerate(names)]) if names else np.zeros(0, np.int32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:  # 传文件对象，np.savez 才不会自己补 .npz 后缀
            np.savez(f, version=np.array([STORE_VERSION], np.int32), names=np.array(names, dtype=str), embs=embs, owner=owner)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class SpeakerGate:
    def __init__(self, threshold: float = DEFAULT_THRESHOLD, store: Path = STORE, model: Path = SPK_MODEL) -> None:
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=1, provider="cpu")
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.model = model
        self.threshold = threshold
        self.store = store
        self.legacy = False
        self._db: dict[str, np.ndarray] = {}  # 整体替换，不原地改：前端线程打分时拿到的总是完整的一份
        self._lock = threading.Lock()  # 只串行化写盘/换库
        self._load()

    @property
    def dim(self) -> int:
        return self._ex.dim

    # ---- 提取 ----
    def embed(self, samples: np.ndarray, sr: int = SR) -> np.ndarray:
        s = self._ex.create_stream()
        s.accept_waveform(sr, samples.astype(np.float32, copy=False))
        s.input_finished()
        v = np.asarray(self._ex.compute(s), dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    # ---- 注册库 ----
    def _load(self) -> None:
        if not self.store.exists():
            self._db, self.legacy = {}, False
            return
        try:
            self._db, self.legacy = load_store(self.store, self._ex.dim)
        except (OSError, ValueError) as e:  # 库和模型不配套或文件坏了：按空库处理（守护进程会告警）
            log.warning("声纹库不可用，按空库处理，请重新注册：%s", e)
            self._db, self.legacy = {}, False

    def reload(self) -> None:
        with self._lock:
            self._load()

    def names(self) -> list[str]:
        return list(self._db)

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._db.items()}

    def refs(self, name: str) -> np.ndarray | None:
        return self._db.get(name)

    def set_person(self, name: str, embs: np.ndarray) -> None:
        """用 embs（[N, dim]，已归一化）替换这个人的全部样本，整库一次写盘。"""
        with self._lock:
            db = dict(self._db)
            db[name] = np.asarray(embs, dtype=np.float32)
            save_store(self.store, db)
            self._db, self.legacy = db, False

    def backup(self) -> Path | None:
        """保存前调用：首次建 pre-reenroll 备份（永不覆盖），每次刷新 .prev。库不存在返回 None。"""
        if not self.store.exists():
            return None
        b = backup_path(self.store)
        if not b.exists():
            shutil.copy2(self.store, b)
        shutil.copy2(self.store, self.store.with_name(f"{self.store.stem}.prev.npz"))
        return b

    def rollback(self) -> bool:
        """用 pre-reenroll 备份替换当前库并重载。没有备份返回 False。"""
        b = backup_path(self.store)
        if not b.exists():
            return False
        with self._lock:
            tmp = self.store.with_name(f"{self.store.name}.{os.getpid()}.tmp")
            try:
                shutil.copy2(b, tmp)
                os.replace(tmp, self.store)
            finally:
                tmp.unlink(missing_ok=True)
            self._load()
        return True

    # ---- 识别 ----
    def identify(self, samples: np.ndarray, sr: int = SR) -> tuple[str | None, float]:
        db = self._db
        if not db:
            return None, 0.0
        v = self.embed(samples, sr)
        best, score = None, -1.0
        for name, refs in db.items():
            c = score_against(v, refs)
            if c > score:
                best, score = name, c
        return (best if score >= self.threshold else None), score


if __name__ == "__main__":
    # 自检：同一说话人的两段应该相似，不同说话人应该低（模型对错、音频通不通一眼看出）
    from guanjia.asr import MODEL_DIR
    from guanjia.vad import read_wav_16k

    g = SpeakerGate(store=Path("tmp/speakers_selftest.npz"))
    wavs = sorted((MODEL_DIR / "test_wavs").glob("*.wav"))
    embs = {w.name: g.embed(read_wav_16k(w)) for w in wavs}
    names = list(embs)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            print(f"{names[i]} vs {names[j]}: cos={float(np.dot(embs[names[i]], embs[names[j]])):.3f}")
    # 把 0.wav 的前半段注册为 A，用后半段识别
    a = read_wav_16k(wavs[0])
    g.set_person("A", g.embed(a[: len(a) // 2])[None])
    print("identify second half of", wavs[0].name, "->", g.identify(a[len(a) // 2:]))
    print("identify", wavs[1].name, "->", g.identify(read_wav_16k(wavs[1])))
