"""假触发/幻觉段判定（纯函数，可单测；尚未接入 voice.py）：
  is_hollow_segment —— 被音箱 DSP 噪声门"掏空"的段（识别之前判）；
  is_unconfirmed_by_stream —— 流式没出字而最终出了字（识别之后判，2024 版 SenseVoice 的幻觉防线）。

以下是 is_hollow_segment 的背景。

背景：漫步者音箱的噪声门会把弱音置成精确的数字零。残余底噪触发 VAD 时，切出来的段一大半是 0、
剩下是极弱的底噪，SenseVoice 会把它幻觉成「嗯/唉/我/Yeah」。实测（pool 里 39 段碎音/空段）：
  数字零占比 ≥30% 且 20ms 帧 RMS 的 90 分位 <0.02 的，占 24/39；主人真正说的指令段（≥3 字）里只有 2 条
  落进这个区间，而且那 2 条本身就是 TTS 回灌/乱码，不是指令。
"""

from __future__ import annotations

import re

import numpy as np

HOLLOW_ZERO_FRAC = 0.3  # 精确为 0 的样本占比下限
HOLLOW_P90_MAX = 0.02  # 增益后（送 ASR 的那份）20ms 帧 RMS 的 90 分位上限；真实指令段中位数约 0.085
_FRAME = 320  # 16k 下 20ms


def _p90_frame_rms(x: np.ndarray) -> float:
    k = len(x) // _FRAME
    if k == 0:
        return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    rms = np.sqrt((x[: k * _FRAME].reshape(k, _FRAME) ** 2).mean(axis=1))
    return float(np.percentile(rms, 90))


_NON_WORD_RE = re.compile(r"[\W_]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def is_unconfirmed_by_stream(final_text: str, stream_text: str | None, *, min_chars: int = 2) -> bool:
    """二次识别出了"像一句话"的中文、流式识别一个字都没出 → 疑似对近乎静音段的幻觉（走走了吧/我我不知道）。

    2024 版 SenseVoice 比 2025 版更爱在静音段"编"出几个字；而流式模型在这类段上几乎总是空（pool 里 37 个噪声段 0 个有字）。
    stream_text 为 None 表示不知道流式结果，不判。纯英文（流式是中文模型，本来就出不了）不判。
    """
    if stream_text is None:
        return False
    core = _NON_WORD_RE.sub("", final_text)
    if len(core) < min_chars or not _HAN_RE.search(core):
        return False
    return not _NON_WORD_RE.sub("", stream_text)


def is_hollow_segment(proc: np.ndarray, raw: np.ndarray | None = None, *,
                      zero_frac_min: float = HOLLOW_ZERO_FRAC, p90_max: float = HOLLOW_P90_MAX) -> bool:
    """这一段是不是"噪声门掏空的假触发"。

    Args:
        proc: 送 ASR 的 16k 增益后音频（VAD 段）。
        raw: 与该段对齐的设备原始音频（可选）。给了就用它算零占比——proc 经重采样/滤波后零可能不再精确；
            空数组按没给处理。
    Returns:
        True=建议丢弃（不进 ASR/声纹）；空段返回 False（没有依据就不拦）。
    """
    if len(proc) == 0:
        return False
    ref = raw if raw is not None and len(raw) > 0 else proc
    zero_frac = float(np.mean(ref == 0))
    return zero_frac >= zero_frac_min and _p90_frame_rms(proc) < p90_max
