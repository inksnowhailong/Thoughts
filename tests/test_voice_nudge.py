"""「只说没做」兜底用例：纯函数（承诺词 + 零工具调用）和守护进程接线（追发一次、不连环追、被打断/出错/有工具/问问题不追）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_voice_nudge.py
"""
import queue
import threading
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia.status import Health
from guanjia.turns import NUDGE_TEXT, PROMISE_WORDS, SayItem, TurnResult, has_promise, needs_nudge


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


# ---- 纯函数 ----
check(needs_nudge("我先查一下系统里当前用的音频设备。", 0), "promise + zero tools -> nudge")
check(needs_nudge("我看看这台电脑上能不能找到。", 0), "我看看 -> nudge")
check(not needs_nudge("我先查一下。", 1), "promise but tool was called -> no nudge")
check(not needs_nudge("我先查一下。", 0, aborted=True), "aborted -> no nudge")
check(not needs_nudge("我先查一下。", 0, error="网关：boom"), "error -> no nudge")
check(not needs_nudge("要我打开音箱软件吗？", 0), "question without promise -> no nudge")
check(not needs_nudge("已经打开了。", 0), "plain result -> no nudge")
check(not needs_nudge("", 0), "empty reply -> no nudge")
check(has_promise("稍等一下哦") and has_promise("好的，马上办") and not has_promise("现在三点。"), "has_promise basics")
check(all(w for w in PROMISE_WORDS) and "我先" in PROMISE_WORDS and "我帮你看" in PROMISE_WORDS, "promise word list constant")
check("工具" in NUDGE_TEXT and "如实" in NUDGE_TEXT, "nudge text asks to call tools")


# ---- 守护进程接线 ----
class FakeTTS:
    def abort(self) -> None:
        pass


class ScriptRunner:
    """按剧本依次回复；回调里可模拟工具调用 / 打断 / 出错。"""

    def __init__(self, daemon, script) -> None:
        self.d = daemon
        self.script = list(script)  # [(reply, tools, aborted, error)]
        self.texts: list[str] = []

    def run_reply(self, text, gen):
        self.texts.append(text)
        reply, tools, aborted, error = self.script.pop(0)
        for _ in range(tools):
            self.d._on_tool({"ts": "", "tool": "exec", "level": "L1", "decision": "allow", "state": "done", "summary": "x"})
        if aborted:
            self.d._gen += 1  # 模拟被打断：换代
        return TurnResult(reply=reply, spoken=[reply], aborted=aborted, error=error)


def make_daemon(script):
    d = object.__new__(voice.VoiceDaemon)
    d._lock, d._stop_evt = threading.Lock(), threading.Event()
    d._gen, d.state = 0, "LISTENING"
    d.play_q, d.text_q = queue.Queue(), queue.Queue()
    d._muted, d._paused = threading.Event(), threading.Event()
    d._play_buf, d._play_pos = np.zeros(0, dtype=np.float32), 0
    d._earcon = np.ones(4, dtype=np.float32)
    d._turn_first, d._turn_t0 = False, 0.0
    d.tray, d.last_error, d.sleep_state = None, "", "awake"
    d.tts, d.brain = FakeTTS(), SimpleNamespace(last_run_id=None)
    d.health = Health(True, True)
    d.runtime = SimpleNamespace(state="ACTIVE")
    d._exit_code = None
    d.runner = ScriptRunner(d, script)
    return d


voice.AUDIT_LAG_SEC = 0  # 单测不等审计跟随的轮询间隔

# 1. 只说没做 -> 追一次；追发的那一轮再说「我先」也不再追
d = make_daemon([("我先查一下系统里当前用的音频设备。", 0, False, None), ("我再看看。", 0, False, None)])
d._handle_item("帮我把 HALO 音响的驱动打开")
check(d.runner.texts == ["帮我把 HALO 音响的驱动打开", NUDGE_TEXT], "nudge sent once with NUDGE_TEXT, never twice")

# 2. 追发后模型调了工具 -> 正常结束
d = make_daemon([("我先查一下。", 0, False, None), ("已经打开了。", 2, False, None)])
d._handle_item("打开音箱软件")
check(len(d.runner.texts) == 2 and d.runner.script == [], "nudge turn with tools finishes normally")

# 3. 本轮有工具调用 -> 不追
d = make_daemon([("我先查一下，好了，已经打开。", 1, False, None)])
d._handle_item("打开音箱软件")
check(len(d.runner.texts) == 1, "tool called in turn -> no nudge")

# 4. 被打断 / 出错 / 问问题 / 没承诺词 -> 不追
for script, what in ((("我先查一下。", 0, True, None), "aborted"), (("我先查一下。", 0, False, "网关：boom"), "error"),
                     (("要打开音箱软件吗？", 0, False, None), "question"), (("已经打开了。", 0, False, None), "plain")):
    d = make_daemon([script])
    d._handle_item("打开音箱软件")
    check(len(d.runner.texts) == 1, f"{what} -> no nudge")

# 5. /say 不追；主人又说了话（队列里有新内容）不追；静音不追
d = make_daemon([("我先查一下。", 0, False, None)])
d.runner.run_say = lambda text, gen: TurnResult(reply="我先查一下。", spoken=["我先查一下。"])
d._handle_item(SayItem("我先查一下。"))
check(d.runner.texts == [], "say turn never nudges")
d = make_daemon([("我先查一下。", 0, False, None)])
d.text_q.put("主人的新一句")
d._handle_item("打开音箱软件")
check(len(d.runner.texts) == 1, "owner already queued a new utterance -> no nudge")
d = make_daemon([("我先查一下。", 0, False, None)])
d._muted.set()
d._handle_item("打开音箱软件")
check(len(d.runner.texts) == 1, "muted -> no nudge")

# 6. 下一轮重新计数：上一轮的工具调用不带到下一轮
d = make_daemon([("好的。", 3, False, None), ("我先查一下。", 0, False, None), ("做完了。", 1, False, None)])
d._handle_item("第一句")
d._handle_item("第二句")
check(len(d.runner.texts) == 3, "tool counter resets per turn (second turn nudged)")

print("ALL PASS test_voice_nudge")
