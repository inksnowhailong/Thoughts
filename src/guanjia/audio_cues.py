"""本地提示音：主人说完一句、声纹放行后立刻播放，压住等大脑回话的空白（"开口慢"的体感主要来自这段静默）。"""

from __future__ import annotations

import numpy as np

EARCON_AMP = 0.2  # 峰值幅度（满幅 1.0）；音箱条音量本身可调，这里取不刺耳的中等值


def _tone(sr: int, freq: float, ms: int, amp: float) -> np.ndarray:
    """一段带 8ms 淡入淡出的正弦音（防爆音）。"""
    n = sr * ms // 1000
    i = np.arange(n)
    ramp = max(1, sr * 8 // 1000)
    env = np.minimum(1.0, np.minimum(i, n - 1 - i) / ramp)
    return (np.sin(2 * np.pi * freq * i / sr) * env * amp).astype(np.float32)


def earcon(sr: int, amp: float = EARCON_AMP) -> np.ndarray:
    """两声上行短音：880Hz 90ms + 静音 20ms + 1320Hz 110ms，共 0.22 秒，float32 单声道。"""
    gap = np.zeros(sr * 20 // 1000, dtype=np.float32)
    return np.concatenate([_tone(sr, 880.0, 90, amp), gap, _tone(sr, 1320.0, 110, amp)])
