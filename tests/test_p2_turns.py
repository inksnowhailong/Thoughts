"""P2 TurnRunner 用例（假网关流 / 假 TTS / 假播放队列，不碰音频和网络）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p2_turns.py
"""
import numpy as np

from guanjia.turns import SayItem, TurnRunner


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


class Harness:
    """可注入的假依赖：记录网关流是否读完 / 是否被提前关闭、合成了哪些句子、入队了什么。"""

    def __init__(self, pieces, fail_synth=0, bump_gen_after_enqueue=None, raise_after=None, abort_on_synth=None):
        self.pieces = pieces
        self.fail_synth = fail_synth  # 前 N 次合成抛错
        self.bump_gen_after_enqueue = bump_gen_after_enqueue  # 第 N 次入队后模拟"被打断"（换代）
        self.raise_after = raise_after  # 网关流在第 N 段之前抛 ConnectionError
        self.abort_on_synth = abort_on_synth  # 第 N 次合成时模拟打断：先换代再抛错（tts.abort 关掉了请求）
        self.gen = 0
        self.enqueued = []
        self.synth_calls = []
        self.state = {"drained": False, "closed_early": False, "text": None}

    def open_stream(self, text):
        self.state["text"] = text
        try:
            for i, p in enumerate(self.pieces):
                if self.raise_after is not None and i == self.raise_after:
                    raise ConnectionError("boom")
                yield p
            self.state["drained"] = True
        except GeneratorExit:
            self.state["closed_early"] = True
            raise

    def synth(self, text):
        self.synth_calls.append(text)
        n = len(self.synth_calls)
        if n <= self.fail_synth:
            raise RuntimeError("tts down")
        if self.abort_on_synth == n:
            self.gen += 1
            raise RuntimeError("closed by abort")
        return np.zeros(10, dtype=np.float32), 24000

    def enqueue(self, gen, pcm, sr):
        self.enqueued.append((gen, len(pcm), sr))
        if self.bump_gen_after_enqueue == len(self.enqueued):
            self.gen += 1

    def runner(self):
        return TurnRunner(self.open_stream, self.synth, self.enqueue, lambda: self.gen, first_max_chars=20, max_tts_fails=2)


# 1. 正常一轮
h = Harness(["你好，", "我是管家。今天", "天气不错。"])
r = h.runner().run_reply("[语音 2026-10-10 18:05 周六] 你好", 0)
check(r.spoken == ["你好，我是管家。", "今天天气不错。"], "normal turn speaks sentence by sentence")
check(r.reply == "你好，我是管家。今天天气不错。" and r.error is None and not r.aborted, "normal turn reply text, no error")
check(h.enqueued == [(0, 10, 24000), (0, 10, 24000)], "enqueued with the turn generation")
check(h.state["text"] == "[语音 2026-10-10 18:05 周六] 你好", "gateway gets the tagged text")
check(h.state["drained"] and not h.state["closed_early"], "gateway stream read to the end")

# 2. 打断：第一句入队后换代，剩下的不再合成，但网关流照样读完、绝不提前关闭
h = Harness(["你好，", "我是管家。今天", "天气不错。"], bump_gen_after_enqueue=1)
r = h.runner().run_reply("x", 0)
check(h.synth_calls == ["你好，我是管家。"], "after barge-in no more synthesis")
check(r.aborted and r.unspoken == ["今天天气不错。"], "skipped sentence recorded as unspoken")
check(h.state["drained"] and not h.state["closed_early"], "barge-in keeps draining, never closes the gateway stream")

# 3. TTS 连续失败两次：第三句不再尝试，流照样读完
h = Harness(["第一句话。", "第二句话。", "第三句话。"], fail_synth=99)
r = h.runner().run_reply("x", 0)
check(len(h.synth_calls) == 2 and len(r.unspoken) == 3, "stop synthesizing after 2 consecutive TTS failures")
check(r.error is not None and r.error.startswith("TTS"), "TTS failure reported")
check(h.state["drained"] and not h.state["closed_early"], "TTS failure keeps draining")

# 4. 打断发生在合成中（tts.abort 让请求抛错）：算打断不算 TTS 失败
h = Harness(["第一句话。", "第二句话。", "第三句话。"], abort_on_synth=2)
r = h.runner().run_reply("x", 0)
check(r.spoken == ["第一句话。"] and r.unspoken == ["第二句话。", "第三句话。"], "abort during synthesis skips the rest")
check(r.aborted and r.error is None, "abort during synthesis is not an error")

# 5. 静默标记、Markdown
h = Harness(["NO_", "REPLY"])
r = h.runner().run_reply("x", 0)
check(h.synth_calls == [] and r.spoken == [] and r.unspoken == [] and r.reply == "NO_REPLY", "NO_REPLY is not spoken")
h = Harness(["**好的**，", "马上办。"])
r = h.runner().run_reply("x", 0)
check(r.spoken == ["好的，马上办。"], "markdown stripped before TTS")

# 6. 网关中途出错
h = Harness(["你好，我是管家。", "后面"], raise_after=1)
r = h.runner().run_reply("x", 0)
check(r.spoken == ["你好，我是管家。"] and r.error is not None and "boom" in r.error and r.error.startswith("网关"),
      "gateway error after first sentence")

# 7. /say：不进网关，直接合成
h = Harness([])
r = h.runner().run_say("今天降温了，多穿点。", 0)
check(r.spoken == ["今天降温了，多穿点。"] and h.state["text"] is None, "say does not call the gateway")
check(r.reply == "今天降温了，多穿点。", "say reply is the text")
h = Harness([])
r = h.runner().run_say("这句被打断了。", -1)
check(r.aborted and r.unspoken == ["这句被打断了。"] and h.synth_calls == [], "say skipped when generation changed")
check(SayItem("念这句").text == "念这句", "SayItem holds text")

print("ALL PASS test_p2_turns")
