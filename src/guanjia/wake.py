"""一句话要不要进主线：碎音过滤 → 唤醒词（免喊窗口内可省）→ 声纹门槛只管没喊唤醒词的话。纯逻辑，可单测。

唤醒词按拼音容错匹配：识别常把"管家"写成"关家/管加/光价"，所以逐字比读音而不是比字形——
唤醒词按词语取它本来的读音（"管家"= guan jia），识别文字里的字取全部读音（识别写成的多音字都算），
再做常见的模糊音归并（zh/ch/sh→z/c/s、n→l、后鼻音 ng→n），唤醒词连续每个字都对上才算命中；
4 个字及以上的唤醒词允许错一个字。唤醒词那一侧不取冷僻读音：家 还读 jie/gu，取了就会让"关节/光顾/广告"误命中。

    "关家，打开记事本"  ──命中"管家"──> 送主线"打开记事本"
    "管家"              ──只有唤醒词──> 应答"在呢"并打开免喊窗口
    "打开记事本"        ──没有唤醒词──> 不理（免喊窗口内照常送）
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from pypinyin import Style, pinyin

FILLER_CHARS = set("嗯啊哦呃唉哎诶欸喂嘿哈呀吧呢嘛么哼咦噢哇呵啦咯嗨额")  # 纯语气词片段不当指令
SHORT_ANSWERS = set("好行对要是不停")  # 免喊窗口里允许的单字回答（"要我打开吗？""好"）
_PUNCT_RE = re.compile(r"[\s\W_]+")  # 中文标点也算 \W；汉字、字母、数字是 \w
_WORD_RE = re.compile(r"[^\s\W_]+")  # 被标点、空白隔开的一段正文


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


def _word_readings(word: str) -> list[frozenset[str]]:
    """唤醒词每个字的读音：按词语取本来的读音（pypinyin 词库会处理"小乐/长江"这类多音字）。"""
    chars = "".join(ch for ch in word if not _PUNCT_RE.fullmatch(ch))
    pys = pinyin(chars, style=Style.NORMAL, errors=lambda s: list(s))  # 非汉字逐个成项，和 chars 一一对齐
    return [frozenset({_fuzzy(py[0]) if _is_han(ch) else ch.lower()}) for ch, py in zip(chars, pys)]


def _trim_edges(text: str) -> str:
    """去掉两头的标点、空白和单独成段的语气词（「管家呀，开灯」去掉「呀」；「哈尔滨」「附近的酒吧」不动）。"""
    runs = [m for m in _WORD_RE.finditer(text) if not all(ch in FILLER_CHARS for ch in m.group())]
    return text[runs[0].start():runs[-1].end()] if runs else ""


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
        self._patterns = [(w, pat) for w in self.words if (pat := _word_readings(w))]

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
        """去掉第一个唤醒词，以及紧挨在两头的重复唤醒词（「管家管家」）；话中间再出现的同音词是内容，不动。
        返回 (剩下的话, 第一个命中)。"""
        first = self.find(text)
        if first is None:
            return _trim_edges(text), None
        rest = _trim_edges(text[:first.start] + " " + text[first.end:])
        while (hit := self.find(rest)) is not None and (not _trim_edges(rest[:hit.start]) or not _trim_edges(rest[hit.end:])):
            rest = _trim_edges(rest[:hit.start] + " " + rest[hit.end:])
        return rest, first


@dataclass(frozen=True)
class GateDecision:
    verdict: str  # accept / wake_only / ignored / rejected
    stage: str  # 拦在哪一环：filler / speaker / wake；放行时为 ""
    reason: str  # 给人看的原因，面板和日志用
    text: str  # 放行时送主线的话（已去掉唤醒词）
    wake: str = ""  # 原文里命中的唤醒词写法
    spk_checked: bool = False  # 这一句真的拿声纹当了门槛（没喊唤醒词，且分数不是 None）


def decide(text: str, score: float | None, *, threshold: float, matcher: WakeMatcher, wake_required: bool,
           min_chars: int, in_window: bool, allow_short_answer: bool = True) -> GateDecision:
    """一句识别结果的去留。score=None 表示不过声纹（测试注入、没有声纹库）。
    含唤醒词的话不按碎音丢：最少字数调到 3 以上时，只喊「管家」也要能应答。
    allow_short_answer=False：它还在说的时候，"好/对/是"多半是附和，不当成新的一句。"""
    if is_filler(text, min_chars, in_window and allow_short_answer) and matcher.find(text) is None:
        return GateDecision("ignored", "filler", f"碎音/语气词「{core_text(text) or text}」", "")
    rest, hit = matcher.strip(text)
    if hit is not None:  # 喊了唤醒词：声纹分数再低也不拒
        if is_filler(rest, min_chars, True):
            return GateDecision("wake_only", "", f"只喊了「{hit.heard}」", "", hit.heard)
        return GateDecision("accept", "", f"唤醒词「{hit.heard}」", rest, hit.heard)
    if wake_required and not in_window:
        return GateDecision("rejected", "wake", "没有唤醒词", "")
    if score is not None and score < threshold:  # 免喊窗口内，或不需要唤醒词，才用声纹当门槛
        shown = f"{score:.2f}"
        if shown == f"{threshold:.2f}":  # 0.447 四舍五入会显示成"0.45 < 0.45"，改成截到三位
            shown = f"{math.floor(score * 1000) / 1000:.3f}"
        return GateDecision("rejected", "speaker", f"声纹 {shown} < {threshold:.2f}", "", spk_checked=True)
    return GateDecision("accept", "", "免喊窗口内" if in_window else "不需要唤醒词", text,
                        spk_checked=score is not None)
