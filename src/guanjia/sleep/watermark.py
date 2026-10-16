"""Guard 写入的跨入口主线活动水位只读校验器。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

PATH = Path.home() / ".openclaw" / "workspace" / "guanjia" / "main-watermark.json"
SOURCES = {"voice", "wechat", "owner", "unknown"}


@dataclass(frozen=True)
class MainWatermark:
    valid: bool
    reason: str
    last_activity_at: float | None = None
    seq: int | None = None
    source: str | None = None


def read_main_watermark(path: Path = PATH, *, now: float | None = None) -> MainWatermark:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        at, seq, source = raw["last_activity_at"], raw["seq"], raw["source"]
        if raw.get("version") != 1 or raw.get("session") != "agent:main:main":
            raise ValueError("schema")
        if not isinstance(at, (int, float)) or not isinstance(seq, int) or seq < 1 or source not in SOURCES:
            raise ValueError("fields")
        if at > (now if now is not None else time.time()) + 5:
            raise ValueError("future")
        return MainWatermark(True, "ok", float(at), seq, source)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return MainWatermark(False, "主线消息水位不可用或损坏")
