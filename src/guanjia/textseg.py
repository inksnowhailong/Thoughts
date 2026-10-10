"""把流式到达的回复文本切成可朗读的句子：按中文/英文句末标点切，逗号处只在句子已经够长时切，保证第一句尽早出声。

speakable() 再把一句话里念不出来的东西去掉（Markdown 符号、链接、表情、静默标记 NO_REPLY），什么都不剩就不念。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

END = set("。！？!?；;\n")
SOFT = set("，,、：:")

_SILENT_RE = re.compile(r"^\s*NO_REPLY\s*$", re.IGNORECASE)  # OpenClaw 的静默回复标记，整句就是它时不念
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_BULLET_RE = re.compile(r"^\s*(?:[•·]\s*|[-*]\s+|\d+[.、)）]\s+)")  # 行首列表符号；"1.5 度"这类小数不算
_MD_RE = re.compile(r"[*#`>|_~\[\]]+")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


class SentenceSplitter:
    def __init__(self, soft_min_chars: int = 18, hard_max_chars: int = 60, first_soft_min_chars: int = 8,
                 first_hard_max_chars: int | None = None) -> None:
        self.soft_min = soft_min_chars
        self.hard_max = hard_max_chars
        self.first_hard_max = first_hard_max_chars or hard_max_chars  # 第一句的硬上限：无标点时不等满 hard_max 才出声
        self.first_soft_min = first_soft_min_chars  # 第一句在逗号处更早切，让首字尽快出声
        self._buf = ""
        self._emitted = 0

    def push(self, piece: str) -> Iterator[str]:
        """喂一段增量文本，yield 已经完整的句子。"""
        self._buf += piece
        while True:
            cut = self._find_cut()
            if cut is None:
                return
            sent, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if sent:
                self._emitted += 1
                yield sent

    def flush(self) -> Iterator[str]:
        rest, self._buf = self._buf.strip(), ""
        if rest:
            yield rest

    def _find_cut(self) -> int | None:
        first = self._emitted == 0
        soft_min = self.first_soft_min if first else self.soft_min
        hard_max = self.first_hard_max if first else self.hard_max
        for i, ch in enumerate(self._buf):
            if ch in END:
                return i + 1
            if ch in SOFT and i + 1 >= soft_min:
                return i + 1
            if i + 1 >= hard_max:
                return i + 1
        return None


def speakable(sent: str) -> str:
    """一句回复里能念出来的部分；只剩标点、表情或静默标记时返回空串（调用方据此跳过这一句）。"""
    if _SILENT_RE.match(sent):
        return ""
    s = _URL_RE.sub("", sent)
    s = _EMOJI_RE.sub("", s)
    s = _BULLET_RE.sub("", s)
    s = _MD_RE.sub("", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s if _WORD_RE.search(s) else ""


if __name__ == "__main__":
    s = SentenceSplitter()
    stream = ["今天天气不", "错，适合出去走走。", "要不要我帮你查一下附近的公园？", "顺便提醒，", "下午三点有会"]
    for p in stream:
        for sent in s.push(p):
            print("SENT:", sent)
    for sent in s.flush():
        print("FLUSH:", sent)
