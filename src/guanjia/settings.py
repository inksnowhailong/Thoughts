"""语音守护进程的可调参数（唤醒词、免喊窗口、声纹门槛、打断线……）。

存在 F:\\coding\\guanjia\\data\\voice-settings.json（data/ 不进仓库）。面板改了立即写盘并通知守护进程热生效，
手工改文件也会在 2 秒内被主循环发现并生效。文件坏了或某项不合法时保留当前值，不会把守护进程弄崩。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger("guanjia.settings")

SETTINGS_PATH = Path(__file__).resolve().parents[2] / "data" / "voice-settings.json"


@dataclass(frozen=True)
class Settings:
    wake_words: tuple[str, ...] = ("管家",)  # 识别文字里含其一才进主线（按拼音容错匹配）
    wake_required: bool = True  # False = 不要唤醒词（P2 的行为）
    follow_up_sec: float = 20.0  # 它回完最后一句后这么多秒内接着说，不用再喊
    spk_threshold: float = 0.45  # 声纹放行门槛
    barge_rms_min: float = float(os.environ.get("GUANJIA_BARGE_RMS_MIN", "0.004"))  # 打断线下限（原始输入 RMS）
    min_chars: int = 2  # 去掉标点后少于这么多字的片段直接忽略（免喊窗口里"好/行/对/要/是/不/停"例外）
    earcon: bool = True  # 说完话立即放提示音
    ack_text: str = "在呢"  # 只喊唤醒词时的应答（TTS 不在时改放提示音）


_LIMITS = {
    "follow_up_sec": (0.0, 120.0),
    "spk_threshold": (0.0, 0.9),
    "barge_rms_min": (0.001, 0.05),
    "min_chars": (1, 6),
}


def from_dict(data: dict, base: Settings = Settings()) -> Settings:
    """把（部分）字典合并到 base 上并校验；不合法抛 ValueError，信息是给人看的中文。"""
    if not isinstance(data, dict):
        raise ValueError("设置必须是一个 JSON 对象")
    unknown = set(data) - set(asdict(base))
    if unknown:
        raise ValueError(f"不认识的设置项：{', '.join(sorted(unknown))}")
    vals = asdict(base)
    vals["wake_words"] = tuple(vals["wake_words"])
    if "wake_words" in data:
        raw = data["wake_words"]
        words = raw.replace("，", ",").split(",") if isinstance(raw, str) else raw
        if not isinstance(words, (list, tuple)):
            raise ValueError("唤醒词要写成列表或逗号分隔的文字")
        cleaned = tuple(dict.fromkeys(str(w).strip() for w in words if str(w).strip()))
        if not 1 <= len(cleaned) <= 5 or any(len(w) > 8 for w in cleaned):
            raise ValueError("唤醒词要 1～5 个，每个不超过 8 个字")
        if any(len(re.sub(r"[\s\W_]", "", w)) < 2 for w in cleaned):  # 单字按读音容错几乎句句命中；纯标点永远叫不应
            raise ValueError("每个唤醒词至少要 2 个字（标点不算）")
        vals["wake_words"] = cleaned
    for key in ("wake_required", "earcon"):
        if key in data:
            if not isinstance(data[key], bool):
                raise ValueError(f"{key} 只能是 true 或 false")
            vals[key] = data[key]
    for key, (lo, hi) in _LIMITS.items():
        if key in data:
            try:
                v = int(data[key]) if key == "min_chars" else float(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 要是数字") from None
            if not lo <= v <= hi:
                raise ValueError(f"{key} 要在 {lo}～{hi} 之间")
            vals[key] = v
    if "ack_text" in data:
        text = str(data["ack_text"]).strip()
        if not 1 <= len(text) <= 10:
            raise ValueError("应答语要 1～10 个字")
        vals["ack_text"] = text
    return replace(base, **vals)


def to_dict(s: Settings) -> dict:
    d = asdict(s)
    d["wake_words"] = list(s.wake_words)
    return d


class SettingsStore:
    """当前设置 + 持久化 + 变更通知。path=None 时只在内存里（测试、半构造的守护进程用）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.current = Settings()
        self._mtime: float | None = None
        self._listeners: list[Callable[[Settings], None]] = []
        self._lock = threading.Lock()
        if path is not None:
            self.reload()

    def subscribe(self, fn: Callable[[Settings], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn(self.current)
            except Exception:  # noqa: BLE001
                log.exception("settings listener error")

    def reload(self) -> bool:
        """从文件读；文件不存在用默认值；坏文件保留当前值。返回设置是否变了。"""
        if self.path is None or not self.path.exists():
            return False
        mtime = None
        try:
            mtime = self.path.stat().st_mtime
            new = from_dict(json.loads(self.path.read_text(encoding="utf-8-sig")), Settings())
        except (OSError, ValueError) as e:
            self._mtime = mtime  # 记下坏文件的时间：没再改之前不重读，免得每 2 秒打一行警告
            log.warning("voice-settings.json 读不了，保留当前设置：%s", e)
            return False
        self._mtime = mtime
        changed = new != self.current
        self.current = new
        if changed:
            log.info("settings loaded: %s", to_dict(new))
        return changed

    def maybe_reload(self) -> bool:
        """主循环每 2 秒调一次：文件被手工改过就重读并通知。"""
        if self.path is None or not self.path.exists():
            return False
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        with self._lock:
            changed = self.reload()
        if changed:
            self._notify()
        return changed

    def update(self, patch: dict) -> Settings:
        """面板改设置：校验 → 原子写盘 → 通知。不合法抛 ValueError。"""
        with self._lock:
            new = from_dict(patch, self.current)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                try:
                    tmp.write_text(json.dumps(to_dict(new), ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(tmp, self.path)
                except OSError:
                    tmp.unlink(missing_ok=True)
                    raise
                self._mtime = self.path.stat().st_mtime
            self.current = new
        log.info("settings updated: %s", to_dict(new))
        self._notify()
        return new
