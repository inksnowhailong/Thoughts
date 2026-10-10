"""一句话要不要进主线：碎音过滤 → 声纹门槛 → 唤醒词（免喊窗口内可省）。纯逻辑，可单测。

唤醒词按拼音容错匹配：识别常把"管家"写成"关家/管加/光价"，所以逐字比读音而不是比字形——
每个字取全部读音（多音字都算），再做常见的模糊音归并（zh/ch/sh→z/c/s、n→l、后鼻音 ng→n），
唤醒词连续每个字都对上才算命中；4 个字及以上的唤醒词允许错一个字。

    "关家，打开记事本"  ──命中"管家"──> 送主线"打开记事本"
    "管家"              ──只有唤醒词──> 应答"在呢"并打开免喊窗口
    "打开记事本"        ──没有唤醒词──> 不理（免喊窗口内照常送）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from pypinyin import Style, pinyin

FILLER_CHARS = set("嗯啊哦呃唉哎诶欸喂嘿哈呀吧呢嘛么哼咦噢哇呵啦咯嗨额")  # 纯语气词片段不当指令
SHORT_ANSWERS = set("好行对要是不停")  # 免喊窗口里允许的单字回答（"要我打开吗？""好"）
_PUNCT_RE = re.compile(r"[\s\W_]+")  # 中文标点也算 \W；汉字、字母、数字是 \w
_EDGE_RE = re.compile(r"^[\s\W_啊呀呢吧嘛哈]+|[\s\W_啊呀呢吧嘛哈]+$")  # 去掉唤醒词后两头残留的标点和语气词


def _is_han(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _fuzzy(py: str) -> str:
    """模糊音归并：zh/ch/sh→z/c/s，声母 n→l，后鼻音 ang/eng/ing/ong→an/en/in/on。"""
    for a, b in (("zh", "z"), ("ch", "c"), ("sh", "s")):
        if py.startswith(a):
            py = b + py[2:]
            break
    if py.startswith("n"):
        py = "l" + py[1:]
    if py.endswith("ng"):
        py = py[:-1]
    return py


@lru_cache(maxsize=4096)
def _readings(ch: str) -> frozenset[str]:
    if not _is_han(ch):
        return frozenset({ch.lower()})
    return frozenset(_fuzzy(p) for p in pinyin(ch, style=Style.NORMAL, heteronym=True)[0])


def core_text(text: str) -> str:
    """去掉标点和空白后的正文。"""
    return _PUNCT_RE.sub("", text)


def is_filler(text: str, min_chars: int, in_window: bool = False) -> bool:
    """碎音判定：太短、或全是语气词。免喊窗口里"好/行/对/要/是/不/停"这类单字回答不算碎音。"""
    core = core_text(text)
    if in_window and len(core) == 1 and core in SHORT_ANSWERS:
        return False
    return len(core) < min_chars or all(ch in FILLER_CHARS for ch in core)


@dataclass(frozen=True)
class WakeHit:
    start: int  # 在原文里的起止下标（含 start，不含 end）
    end: int
    word: str  # 命中的是哪个唤醒词
    heard: str  # 原文里实际写成什么（例如"关家"）


class WakeMatcher:
    def __init__(self, words: tuple[str, ...] | list[str]) -> None:
        self.words = tuple(words)
        self._patterns = [(w, [_readings(ch) for ch in w if not ch.isspace()]) for w in self.words]

    def find(self, text: str) -> WakeHit | None:
        """在识别文字里找唤醒词，返回最靠前的一个；找不到返回 None。"""
        chars = [(i, ch) for i, ch in enumerate(text) if not _PUNCT_RE.fullmatch(ch)]
        readings = [_readings(ch) for _, ch in chars]
        best: WakeHit | None = None
        for word, pat in self._patterns:
            n = len(pat)
            allowed = 1 if n >= 4 else 0
            for s in range(len(chars) - n + 1):
                miss = sum(1 for k in range(n) if not (pat[k] & readings[s + k]))
                if miss <= allowed:
                    start, end = chars[s][0], chars[s + n - 1][0] + 1
                    if best is None or start < best.start:
                        best = WakeHit(start, end, word, text[start:end])
                    break
        return best

    def strip(self, text: str) -> tuple[str, WakeHit | None]:
        """去掉所有唤醒词（以及两头残留的标点、语气词），返回 (剩下的话, 第一个命中)。"""
        first = None
        while True:
            hit = self.find(text)
            if hit is None:
                break
            first = first or hit
            text = (text[:hit.start] + " " + text[hit.end:]).strip()
        return _EDGE_RE.sub("", text).strip(), first


@dataclass(frozen=True)
class GateDecision:
    verdict: str  # accept / wake_only / ignored / rejected
    stage: str  # 拦在哪一环：filler / speaker / wake；放行时为 ""
    reason: str  # 给人看的原因，面板和日志用
    text: str  # 放行时送主线的话（已去掉唤醒词）
    wake: str = ""  # 原文里命中的唤醒词写法


def decide(text: str, score: float | None, *, threshold: float, matcher: WakeMatcher, wake_required: bool,
           min_chars: int, in_window: bool) -> GateDecision:
    """一句识别结果的去留。score=None 表示不过声纹（测试注入、没有声纹库）。"""
    if is_filler(text, min_chars, in_window):
        return GateDecision("ignored", "filler", f"碎音/语气词「{core_text(text) or text}」", "")
    if score is not None and score < threshold:
        return GateDecision("rejected", "speaker", f"声纹 {score:.2f} < {threshold:.2f}", "")
    rest, hit = matcher.strip(text)
    if hit is None:
        if wake_required and not in_window:
            return GateDecision("rejected", "wake", "没有唤醒词", "")
        return GateDecision("accept", "", "免喊窗口内" if in_window else "不需要唤醒词", text)
    if is_filler(rest, min_chars, True):
        return GateDecision("wake_only", "", f"只喊了「{hit.heard}」", "", hit.heard)
    return GateDecision("accept", "", f"唤醒词「{hit.heard}」", rest, hit.heard)
