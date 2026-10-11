"""口语整形（语音轮「要念出来的文字」这一侧，纯函数，可单测）。

模型守「语音轮最多两句、每句≤30 字、无 Markdown」的比例只有 30–60%，所以在念之前再整形一道：
  - 去 Markdown / 表情：粗体、标题、列表符、反引号去掉，链接 [文字](网址) 只留文字，括号里的英文旁注原样保留；
  - 只念前 N 句（默认 2，Settings.speech_max_sentences；0 = 不整形）：第 N+1 句起不再送 TTS，但回复照常收完
    （面板、记录、「只说没做」追问判定用的仍是完整回复），没念的句子进 TurnResult.unspoken，带 SHAPED_PREFIX 前缀；
    例外：句数用完后的块只要含问号或确认类词（ASK_MARKS / ASK_WORDS），照样念——主人需要听到「要你放行」这类话；
    一旦某块因此被保留，同一句的后续块也保留（不念半句），到句末为止；
  - 一句超过 30 字，就在逗号 / 顿号处切短分几次念；切不开（30 字之内没有逗号）就保留整句，不拦腰断词。

接入点是 turns.TurnRunner.run_reply 的切句路径：SentenceSplitter 切出一块就整形一块，边生成边念，不拖慢首字。
「一句」按句末标点（。！？；换行）数：SentenceSplitter 为了让首句尽早出声，也会在逗号处切块，这类块不算一句的结束。
"""

from __future__ import annotations

import re

from guanjia.textseg import END, speakable

SHAPED_PREFIX = "（整形省略）"  # unspoken 里被整形省略的句子的前缀，和「被打断 / TTS 失败」没念出的区分开
MAX_CHARS = 30  # 一块（一次送 TTS 的话）的字数上限，超过且有逗号 / 顿号就切短
ASK_MARKS = ("？", "?")  # 句数用完之后，含这些的块照样念（主人需要听到的提问）
ASK_WORDS = ("确认", "放行", "允许", "审批", "同意", "要不要", "可以吗")  # 同上：需要主人回话 / 放行的确认类词
_CUT = set("，,、")  # 切短只在这些位置；冒号、分号不切（冒号后常是列举，分号是句末）

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def strip_links(text: str) -> str:
    """Markdown 链接只留文字、图片整个去掉。裸网址由 textseg.speakable 去。"""
    return _LINK_RE.sub(r"\1", _IMAGE_RE.sub("", text))


def to_speech(text: str) -> str:
    """一块回复里能念的部分：去链接 / Markdown / 表情 / 列表符；什么都不剩返回空串。括号里的英文旁注不动。"""
    return speakable(strip_links(text))


def is_ask(s: str) -> bool:
    """这块话是不是在向主人提问 / 要主人确认（含问号，或含确认类词）。"""
    return any(m in s for m in ASK_MARKS) or any(w in s for w in ASK_WORDS)


def split_long(s: str, max_chars: int = MAX_CHARS) -> list[str]:
    """超过 max_chars 的话在逗号 / 顿号处切成几块：每块尽量长但不超过 max_chars；
    max_chars 之内一个逗号都没有就把到下一个逗号为止的整段保留（宁可长一点也不断词）。"""
    s = s.strip()
    if len(s) <= max_chars:
        return [s] if s else []
    out: list[str] = []
    while len(s) > max_chars:
        cut = max((i + 1 for i, ch in enumerate(s[:max_chars]) if ch in _CUT), default=0)
        if cut == 0:  # 30 字内没有可切的位置：找下一个，没有就整句保留
            cut = next((i + 1 for i, ch in enumerate(s) if ch in _CUT), 0)
            if cut == 0 or cut >= len(s):
                break
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


class SpeechShaper:
    """一轮回复的整形状态（每轮新建一个）：shape(块) 返回这块该念的几小块和被省略的文字。"""

    def __init__(self, max_sentences: int = 2, max_chars: int = MAX_CHARS) -> None:
        self.max_sentences = max_sentences
        self.max_chars = max_chars
        self.sentences = 0  # 原回复里已读完的句数（句末标点结尾的块）
        self.chars = 0  # 原回复整形前的字数（去 Markdown 后）
        self.spoken_done = 0  # 其中念出来的句数
        self.omitted = 0  # 被省略的块数
        self.split = 0  # 因超长多切出来的块数
        self.kept = 0  # 句数用完后因含提问 / 确认词而照样念的块数（含同一句里跟着保留的后续块）
        self._keeping = False  # 正在保留当前这一句的后续块
        self._open_any = False  # 当前这句已经来过块、还没遇到句末
        self._open_spoken = False  # 当前这句有块被念出来

    def shape(self, chunk: str, ends: bool | None = None) -> tuple[list[str], str]:
        """chunk 是 SentenceSplitter 切出的一块原文。返回 (要念的块们, 被省略的文字)；两者至多一个非空。
        ends=这一块是不是在句末标点（含换行）处切的：SentenceSplitter 切块时会去掉末尾空白，换行结尾的块（列表项）
        从文字上看不出句末，所以由调用方告知；None 时按块的最后一个字符判断。
        去掉 Markdown 后什么都不剩的块（只有 --- 、表情）既不念也不算一句。"""
        s = to_speech(chunk)
        if not s:
            return [], ""
        if ends is None:
            ends = chunk.rstrip()[-1:] in END
        self.chars += len(s)
        self._open_any = True
        quota_full = bool(self.max_sentences) and self.spoken_done >= self.max_sentences
        keep = quota_full and (self._keeping or is_ask(s))
        pieces: list[str] = []
        if keep:
            pieces = split_long(s, self.max_chars)
            self.split += max(0, len(pieces) - 1)
            self.kept += 1
            self._keeping = not ends
        elif quota_full:
            self.omitted += 1
        else:
            pieces = split_long(s, self.max_chars)
            self.split += max(0, len(pieces) - 1)
            self._open_spoken = True
        if ends:
            self.sentences += 1
            self._open_any = False
            if not quota_full:
                self.spoken_done += 1
            self._open_spoken = False
            self._keeping = False
        return pieces, (s if quota_full and not keep else "")

    def stats(self) -> dict | None:
        """整形有没有动过这一轮（省略了句子、或切短了超长句）；没动过返回 None，调用方不记事件。"""
        if not (self.omitted or self.split):
            return None
        return {"sentences": self.sentences + (1 if self._open_any else 0), "chars": self.chars,
                "spoken_sentences": self.spoken_done + (1 if self._open_spoken else 0), "omitted": self.omitted,
                "kept_questions": self.kept, "split": self.split, "limit": self.max_sentences}


def omitted_text(unspoken: list[str]) -> list[str]:
    """从 unspoken 里挑出被整形省略的句子（去掉前缀）。"""
    return [x[len(SHAPED_PREFIX):] for x in unspoken if x.startswith(SHAPED_PREFIX)]


__all__ = ["ASK_MARKS", "ASK_WORDS", "MAX_CHARS", "is_ask", "SHAPED_PREFIX", "SpeechShaper", "omitted_text", "split_long", "strip_links", "to_speech"]
