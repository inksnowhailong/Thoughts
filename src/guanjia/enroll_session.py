"""面板引导的声纹注册会话（纯逻辑）。守护进程在注册模式下把每个 VAD 段交给 feed()，面板轮询 snapshot()。

    recording（按三组提示收样本）──够数──> testing（再说一句自测，不计入）──> ready（等保存/放弃）
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from guanjia.speaker import score_against

SR = 16000
GROUPS = (  # (键, 提示, 例句, 段数)
    ("near", "坐在平时的位置，正常说话", ("管家，明天早上七点叫我起床", "管家，帮我打开网易云放首歌", "今天中午吃什么好呢，我想想"), 4),
    ("far", "走到 2～3 米外，正常音量说", ("管家，现在几点了", "管家，把声音调小一点", "外面是不是要下雨了"), 4),
    ("loud", "大声一点、带点情绪，或者随口说", ("管家！快帮我把游戏打开！", "哎呀烦死了，管家你听见没有", "管家，停一下停一下"), 3),
)
MIN_TOTAL = 8
MAX_SEG_SEC = 10.0
MIN_EFFECTIVE_SEC = 1.5
MAX_ZERO_RATIO = 0.5
MIN_PEAK = 0.03
SESSION_TIMEOUT_SEC = 600.0


def quality(samples: np.ndarray, sr: int = SR) -> tuple[bool, str, dict]:
    """一段能不能拿来注册。数字零是音箱 DSP 噪声门抹掉的部分，不算有效语音。"""
    n = len(samples)
    nz = int(np.count_nonzero(samples))
    info = {"sec": round(n / sr, 2), "effective_sec": round(nz / sr, 2),
            "zero_ratio": round(1 - nz / n, 2) if n else 1.0, "peak": round(float(np.abs(samples).max()), 3) if n else 0.0}
    if n / sr > MAX_SEG_SEC:
        return False, "这段太长了（可能混进了别的声音），说一句 3～5 秒的话", info
    if nz / sr < MIN_EFFECTIVE_SEC:
        return False, f"有效语音只有 {nz / sr:.1f} 秒，请说长一点（3～5 秒）", info
    if info["zero_ratio"] >= MAX_ZERO_RATIO:
        return False, "声音太小，被音箱截掉了一大半，大声一点或近一点", info
    if info["peak"] < MIN_PEAK:
        return False, "声音太小", info
    return True, "", info


@dataclass
class Sample:
    group: str
    emb: np.ndarray
    info: dict


class EnrollSession:
    def __init__(self, name: str, embed: Callable[[np.ndarray], np.ndarray], *, old_refs: np.ndarray | None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self._embed = embed
        self._old = old_refs
        self._now = now
        self.started = now()
        self.state = "recording"
        self.samples: list[Sample] = []
        self.last: dict | None = None
        self.test_score: float | None = None
        self.test_old_score: float | None = None
        self._lock = threading.Lock()  # feed 在前端线程，其余在控制口线程

    def _group(self) -> tuple | None:
        for g in GROUPS:
            if sum(1 for s in self.samples if s.group == g[0]) < g[3]:
                return g
        return None

    def expired(self) -> bool:
        return self._now() - self.started > SESSION_TIMEOUT_SEC

    def feed(self, samples: np.ndarray, *, playing: bool = False) -> dict:
        with self._lock:
            if self.state not in ("recording", "testing"):
                return {"ok": False, "reason": "not_active"}
            if playing:
                self.last = {"ok": False, "reason": "电脑正在出声，这段不算"}
                return self.last
            ok, reason, info = quality(samples)
            if not ok:
                self.last = {"ok": False, "reason": reason, "info": info}
                return self.last
            v = self._embed(samples)
            if self.state == "recording":
                g = self._group()
                self.samples.append(Sample(g[0], v, info))
                if self._group() is None:
                    self.state = "testing"
                self.last = {"ok": True, "group": g[0], "info": info}
            else:
                self.test_score = round(score_against(v, self.embs()), 3)
                if self._old is not None:
                    self.test_old_score = round(score_against(v, self._old), 3)
                self.state = "ready"
                self.last = {"ok": True, "group": "test", "info": info}
            return self.last

    def discard_last(self) -> bool:
        with self._lock:
            if self.state != "recording" or not self.samples:
                return False
            self.samples.pop()
            return True

    def to_testing(self) -> bool:
        """「这些够了」：至少 MIN_TOTAL 段才能去自测。"""
        with self._lock:
            if self.state != "recording" or len(self.samples) < MIN_TOTAL:
                return False
            self.state = "testing"
            return True

    def embs(self) -> np.ndarray:
        return np.stack([s.emb for s in self.samples])

    def snapshot(self) -> dict:
        with self._lock:
            g = self._group() if self.state == "recording" else None
            return {"name": self.name, "state": self.state, "total": len(self.samples), "min_total": MIN_TOTAL,
                    "group": g[0] if g else None, "prompt": g[1] if g else None, "examples": list(g[2]) if g else [],
                    "groups": [{"key": k, "prompt": p, "have": sum(1 for s in self.samples if s.group == k), "need": n}
                               for k, p, _, n in GROUPS],
                    "last": self.last, "test_score": self.test_score, "test_old_score": self.test_old_score,
                    "left_sec": max(0, round(SESSION_TIMEOUT_SEC - (self._now() - self.started)))}
