"""主动环配置：data/vision/proactive.json 的读写与默认值。

九个遗留调参值（议会 §6）全部落在这里，改文件即生效，无需动代码。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 与 vision_loop.VisionLoopConfig 同源：GUANJIA_VISION_ROOT 环境变量优先，缺省硬编码开发目录。
# proactive.json 属运行时配置（data/ 不入库），代码内 _DEFAULTS 保证默认值可追溯；
# 九个调参值改 json 即生效，无需动代码。
_DEFAULT_PATH = Path(os.environ.get(
    'GUANJIA_VISION_ROOT',
    r'F:\coding\guanjia-v2-development\data\vision',
)) / 'proactive.json'

# 默认值：夜间静默 22:00~07:00；bedroom_standby_pan=None 表示卧室朝向未校准，待命功能暂不启用
_DEFAULTS: dict[str, object] = {
    'night_start_hour': 22,
    'night_end_hour': 7,
    'bedroom_standby_pan': None,
    'bedroom_standby_timeout_min': 120,
    'privacy_light_scale': 0.5,
    'privacy_deep_scale': 0.125,
    'leave_window_min': 10,
    'track_edge': 0.22,
    'track_step_duration': 0.25,
    'track_cooldown_sec': 2.0,
    'settle_sec': 1.6,
    'standby_pan_tolerance': 0.05,
    # ---- 触发引擎（C）与身份（A）----
    'proactive_enabled': True,          # 全局开关（JG-8/QC-17）：false 全链停，关停不烧额度
    'review_minimum_score': 0.35,      # 评分门控阈值（预算闸）
    'review_urgent_threshold': 0.9,
    'review_max_per_hour': 10,          # 独立限流（SX-12/JG-19）
    'expression_daily_budget': 3,       # 说话预算（F，JG-10）
    'topic_cooldown_sec': 3600,         # 话题冷却（防重复说）
    'conversation_quiet_min': 5,        # 对话进行中静默窗（QC-22）
    'identity_ttl_min': 10,             # 在场身份缓存 TTL（QC-30）
    'identity_confidence_threshold': 0.55,  # 带称呼开口门控（QC-5）
    'entered_value': 0.6,               # 沿事件候选四因子初值
    'entered_urgency': 0.4,
    'entered_interruption_cost': 0.5,
    'left_value': 0.35,
    'left_urgency': 0.2,
    'left_interruption_cost': 0.5,
}


@dataclass(frozen=True)
class ProactiveConfig:
    """主动环参数；一切数值都是议会遗留调参清单的落点，实测校准后改 json。"""

    path: Path = _DEFAULT_PATH
    values: dict[str, object] = field(default_factory=lambda: dict(_DEFAULTS))

    def __post_init__(self) -> None:
        loaded = self._load()
        merged = dict(_DEFAULTS)
        merged.update(loaded)
        object.__setattr__(self, 'values', merged)

    def _load(self) -> dict[str, object]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def num(self, key: str, fallback: float) -> float:
        value = self.values.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def opt_num(self, key: str) -> float | None:
        """可空数值（如 bedroom_standby_pan 未校准时为 None）。"""
        value = self.values.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def light_scale(self) -> float:
        return max(0.05, min(1.0, self.num('privacy_light_scale', 0.5)))

    @property
    def deep_scale(self) -> float:
        return max(0.02, min(1.0, self.num('privacy_deep_scale', 0.125)))

    @property
    def night_start_hour(self) -> int:
        return int(self.num('night_start_hour', 22)) % 24

    @property
    def night_end_hour(self) -> int:
        return int(self.num('night_end_hour', 7)) % 24


def is_night_hour(hour: int, start: int, end: int) -> bool:
    """判断当前小时是否落在夜间静默窗（支持跨零点，如 22→7）。"""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


__all__ = ['ProactiveConfig', 'is_night_hour']
