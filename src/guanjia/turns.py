"""一轮语音回复的编排（与音频硬件无关，可用假对象单测）。

    网关流 piece ──> SentenceSplitter ──> speakable 过滤 ──> synth ──> enqueue(gen, pcm, sr)

- 打断 = 播放代次变了：剩下的句子不再合成、不再入队，但网关流照样读到结束。
  客户端断开连接会让网关取消这一轮（连同等着的审批），所以这里绝不 break、绝不 close 网关流。
- 每句没念出来的都记进 TurnResult.unspoken（被打断 / TTS 失败），"有字不出声"从此有据可查。
- /say 的内容（SayItem）不进网关，直接切句合成。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import numpy as np

from guanjia.textseg import SentenceSplitter, speakable

log = logging.getLogger("guanjia.turns")


@dataclass
class SayItem:
    """主程序经控制口 /say 让电脑念的一段话（不进网关，直接合成播放）。"""
    text: str


@dataclass
class TurnResult:
    """一轮的结果：网关回复原文、念出来的句子、没念出来的句子、是否被打断、错误。"""
    reply: str = ""
    spoken: list[str] = field(default_factory=list)
    unspoken: list[str] = field(default_factory=list)
    aborted: bool = False
    error: str | None = None


class TurnRunner:
    """把一轮回复变成按句播放的音频；所有依赖都由调用方注入。"""

    def __init__(self, open_stream: Callable[[str], Iterator[str]], synth: Callable[[str], tuple[np.ndarray, int]],
                 enqueue: Callable[[int, np.ndarray, int], None], current_gen: Callable[[], int], *,
                 first_max_chars: int = 20, max_tts_fails: int = 2) -> None:
        self.open_stream = open_stream  # 文本 -> 网关回复增量（生成器）
        self.synth = synth  # 一句 -> (PCM, 采样率)，失败抛异常
        self.enqueue = enqueue  # (代次, PCM, 采样率) -> 进播放队列
        self.current_gen = current_gen  # 当前播放代次；与本轮代次不同即视为被打断
        self.first_max_chars = first_max_chars
        self.max_tts_fails = max_tts_fails

    def run_reply(self, text: str, gen: int) -> TurnResult:
        """一轮语音对话：读完整个网关流，能念的按句合成入队。"""
        res = TurnResult()
        splitter = SentenceSplitter(first_hard_max_chars=self.first_max_chars)
        fails = [0]  # 本轮 TTS 连续失败次数（列表便于 _speak 里修改）
        try:
            for piece in self.open_stream(text):  # 不 break、不 close：打断时也要读完
                res.reply += piece
                for sent in splitter.push(piece):
                    self._speak(sent, gen, res, fails)
            for sent in splitter.flush():
                self._speak(sent, gen, res, fails)
        except Exception as e:  # noqa: BLE001  网关连接/报错：这一轮结束，交给上层记错误
            res.error = f"网关：{type(e).__name__}: {e}"
            res.unspoken.extend(s for s in map(speakable, splitter.flush()) if s)  # 断流时缓冲里半句也记账
        reply = res.reply.strip()
        if reply and reply.upper() != "NO_REPLY" and not (res.spoken or res.unspoken or res.error):
            res.unspoken.append(f"（整轮没有能念的内容）{reply[:60]}")  # 有字但全被过滤：也要在日志里看得见
        return res

    def run_say(self, text: str, gen: int) -> TurnResult:
        """/say：把给定文本切句合成入队，不经过网关。"""
        res = TurnResult(reply=text)
        splitter = SentenceSplitter()
        fails = [0]
        for sent in [*splitter.push(text), *splitter.flush()]:
            self._speak(sent, gen, res, fails)
        return res

    def _speak(self, sent: str, gen: int, res: TurnResult, fails: list[int]) -> None:
        """合成并入队一句；任何异常都在这里吞掉记账，绝不往外抛（外抛会中断读流、导致连接被关闭）。"""
        s = speakable(sent)
        if not s:
            return
        if gen != self.current_gen():
            res.aborted = True
            res.unspoken.append(s)
            return
        if fails[0] >= self.max_tts_fails:
            res.unspoken.append(s)
            return
        try:
            pcm, sr = self.synth(s)
        except Exception as e:  # noqa: BLE001
            res.unspoken.append(s)
            if gen != self.current_gen():  # 打断时 tts.abort() 关掉了请求：算打断，不算 TTS 失败
                res.aborted = True
                return
            fails[0] += 1
            res.error = f"TTS：{type(e).__name__}: {e}"
            log.error("tts error (%d/%d): %s", fails[0], self.max_tts_fails, e)
            return
        fails[0] = 0
        if gen != self.current_gen():
            res.aborted = True
            res.unspoken.append(s)
            return
        try:
            self.enqueue(gen, pcm, sr)
        except Exception as e:  # noqa: BLE001
            res.unspoken.append(s)
            res.error = f"播放：{type(e).__name__}: {e}"
            return
        res.spoken.append(s)
