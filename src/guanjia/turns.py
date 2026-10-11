"""一轮语音回复的编排（与音频硬件无关，可用假对象单测）。

    网关流 piece ──> SentenceSplitter ──> speakable 过滤 ──> synth ──> enqueue(gen, pcm, sr)

- 打断 = 播放代次变了：剩下的句子不再合成、不再入队，但网关流照样读到结束。
  客户端断开连接会让网关取消这一轮（连同等着的审批），所以这里绝不 break、绝不 close 网关流。
- 每句没念出来的都记进 TurnResult.unspoken（被打断 / TTS 失败），"有字不出声"从此有据可查。
- /say 的内容（SayItem）不进网关，直接切句合成。
- on_event(名字, 数据) 给调试面板报进度：first_token / sentence / tts_start / tts_done / tts_fail / enqueued / stream_end。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import numpy as np

from guanjia.textseg import SentenceSplitter, speakable

log = logging.getLogger("guanjia.turns")

# 「只说没做」兜底：回复里出现这些承诺词、而本轮又零工具调用，就追发一次 NUDGE_TEXT（见 voice.VoiceDaemon._handle_item）
PROMISE_WORDS = (
    "我先", "我看看", "我看一下", "我查一下", "我查查", "我来查", "我来看", "稍等", "马上", "这就去", "这就帮", "让我", "我去",
    "我试试", "我帮你查", "我帮你看",
)
# 追发的话（发给网关时由 tag_voice_text 加上「[语音 …]」前缀，分级插件和 AGENTS.md 仍把它当语音轮）
NUDGE_TEXT = "直接调用工具把刚才说的事做完，做完用一句话说结果；做不到就如实说"


def has_promise(reply: str) -> bool:
    """回复里是否含承诺词（「我先查一下」这类说要做、还没做的话）。"""
    return any(w in reply for w in PROMISE_WORDS)


def needs_nudge(reply: str, tool_calls: int, *, aborted: bool = False, error: str | None = None) -> bool:
    """一轮语音回复结束后是否要追问：含承诺词 且 本轮零工具调用；被打断 / 已出错不追。
    只是在问主人问题（含问号、没有承诺词）时没有承诺词，自然不追。"""
    if aborted or error or tool_calls > 0:
        return False
    return has_promise(reply)


@dataclass
class SayItem:
    """主程序经控制口 /say 让电脑念的一段话（不进网关，直接合成播放）。"""
    text: str
    uid: str | None = None  # 调试面板的记录 id


@dataclass
class Heard:
    """识别出、已放行的一句话，带上调试面板的记录 id。"""
    text: str
    uid: str | None = None


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
                 first_max_chars: int = 20, max_tts_fails: int = 2, first_soft_min: int = 8,
                 on_event: Callable[[str, dict], None] | None = None) -> None:
        self.open_stream = open_stream  # 文本 -> 网关回复增量（生成器）
        self.synth = synth  # 一句 -> (PCM, 采样率)，失败抛异常
        self.enqueue = enqueue  # (代次, PCM, 采样率) -> 进播放队列
        self.current_gen = current_gen  # 当前播放代次；与本轮代次不同即视为被打断
        self.first_max_chars = first_max_chars
        self.first_soft_min = first_soft_min  # 首句在逗号处最早第几个字就切：首句越短合成越快
        self.max_tts_fails = max_tts_fails
        self.on_event = on_event

    def _emit(self, name: str, **data) -> None:
        if self.on_event is not None:
            try:
                self.on_event(name, data)
            except Exception:  # noqa: BLE001  面板出错不能影响念
                log.exception("turn on_event error")

    def run_reply(self, text: str, gen: int) -> TurnResult:
        """一轮语音对话：读完整个网关流，能念的按句合成入队。"""
        res = TurnResult()
        splitter = SentenceSplitter(first_hard_max_chars=self.first_max_chars, first_soft_min_chars=self.first_soft_min)
        fails = [0]  # 本轮 TTS 连续失败次数（列表便于 _speak 里修改）
        first = True
        try:
            for piece in self.open_stream(text):  # 不 break、不 close：打断时也要读完
                if first:
                    first = False
                    self._emit("first_token", t=time.time())
                res.reply += piece
                for sent in splitter.push(piece):
                    self._speak(sent, gen, res, fails)
            for sent in splitter.flush():
                self._speak(sent, gen, res, fails)
            self._emit("stream_end", t=time.time(), ok=True)
        except Exception as e:  # noqa: BLE001  网关连接/报错：这一轮结束，交给上层记错误
            res.error = f"网关：{type(e).__name__}: {e}"
            res.unspoken.extend(s for s in map(speakable, splitter.flush()) if s)  # 断流时缓冲里半句也记账
            self._emit("stream_end", t=time.time(), ok=False, error=res.error)
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
        self._emit("sentence", t=time.time(), text=s)
        if gen != self.current_gen():
            res.aborted = True
            res.unspoken.append(s)
            return
        if fails[0] >= self.max_tts_fails:
            res.unspoken.append(s)
            return
        t0 = time.time()
        self._emit("tts_start", t=t0, text=s)
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
            self._emit("tts_fail", t=time.time(), text=s, error=res.error)
            return
        self._emit("tts_done", t=time.time(), text=s, ms=round((time.time() - t0) * 1000))
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
        self._emit("enqueued", t=time.time(), text=s, n=len(res.spoken))
