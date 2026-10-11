"""识别结果的文字规整（纯函数，可单测）。

SenseVoice 2025 版会把英文缩写拆成「H A L O」，旧版 2024 与云端多半输出「HALO」；统一合并，
否则按词匹配的关键词/热词/唤醒词逻辑会失效。
"""

from __future__ import annotations

import re

# 连续 ≥2 个单字母、彼此只隔一个空格，且两端不挨着别的字母：H A L O / Q Q / A I。
# 「I love you」「D 盘」「TempoHub」不满足条件，不会被误合并。
_SPACED_LETTERS_RE = re.compile(r"(?<![A-Za-z])[A-Za-z](?: [A-Za-z])+(?![A-Za-z])")


def merge_spaced_letters(text: str) -> str:
    """把「H A L O」这类被空格拆开的单字母英文合并成「HALO」，其余原样保留。"""
    return _SPACED_LETTERS_RE.sub(lambda m: m.group().replace(" ", ""), text)
