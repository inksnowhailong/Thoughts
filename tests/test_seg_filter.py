r"""被音箱噪声门掏空的假触发段判定（纯函数，尚未接入 voice.py）。

运行：cd /d F:\coding\guanjia && .venv\Scripts\python.exe tests\test_seg_filter.py
"""
import numpy as np

from guanjia.seg_filter import HOLLOW_P90_MAX, HOLLOW_ZERO_FRAC, is_hollow_segment

SR = 16000
rng = np.random.default_rng(0)


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def speech(sec: float, amp: float = 0.3) -> np.ndarray:
    return (rng.standard_normal(int(SR * sec)) * amp).astype(np.float32)


def gated(sec: float, zero_frac: float, amp: float) -> np.ndarray:
    """噪声门式：一部分样本被置成精确 0，剩下的是很弱的底噪。"""
    x = (rng.standard_normal(int(SR * sec)) * amp).astype(np.float32)
    n = int(len(x) * zero_frac)
    x[:n] = 0.0
    return x


check(HOLLOW_ZERO_FRAC == 0.3 and HOLLOW_P90_MAX == 0.02, "thresholds from the pool analysis")
check(not is_hollow_segment(speech(2.0)), "normal speech is kept")
loud_with_zeros = np.concatenate([speech(1.0, 0.3), np.zeros(SR, np.float32)])
check(not is_hollow_segment(loud_with_zeros), "50% zeros but clearly loud speech is kept")
check(is_hollow_segment(gated(1.5, 0.6, 0.004)), "60% zeros + faint noise = hollow")
check(is_hollow_segment(gated(1.5, 0.35, 0.01)), "35% zeros + faint noise = hollow")
check(not is_hollow_segment(gated(1.5, 0.1, 0.004)), "faint but only 10% zeros: not gate-hollowed, keep")
check(not is_hollow_segment(gated(1.5, 0.6, 0.05)), "zeros but loud enough: keep")
check(not is_hollow_segment(np.zeros(0, np.float32)), "empty: no verdict, keep")

# 用原始音频算零占比：增益后的 proc 被重采样/滤波后零可能不再精确，所以允许单独传 raw
proc = (rng.standard_normal(SR) * 0.004).astype(np.float32)  # 没有精确零
raw = gated(1.0, 0.6, 0.004)
check(not is_hollow_segment(proc) and is_hollow_segment(proc, raw), "raw zero fraction is used when given")
check(not is_hollow_segment(proc, np.zeros(0, np.float32)), "empty raw falls back to proc")
# ---------- 流式没出字、最终却有"像一句话"的内容：疑似幻觉 ----------
from guanjia.seg_filter import is_unconfirmed_by_stream  # noqa: E402

check(is_unconfirmed_by_stream("走走了吧", ""), "final has words, stream silent -> suspect")
check(is_unconfirmed_by_stream("我我不知道。", "  "), "whitespace-only stream counts as silent")
check(not is_unconfirmed_by_stream("走走了吧", "走走吧"), "stream also heard something -> keep")
check(not is_unconfirmed_by_stream("嗯", ""), "single char: below min_chars, left to the filler rule")
check(not is_unconfirmed_by_stream("", ""), "empty final: nothing to judge")
check(not is_unconfirmed_by_stream("Here are some coffee shops.", ""), "no Han chars (English): zh streamer cannot confirm, keep")
check(not is_unconfirmed_by_stream("走走了吧", None), "stream unknown (None) -> keep")
check(is_unconfirmed_by_stream("打开记事本", "", min_chars=6) is False, "min_chars respected")
print("ALL PASS")
