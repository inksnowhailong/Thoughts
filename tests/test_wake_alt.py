r"""唤醒词双通道：最终文本没命中、备选文本（流式）在句首命中时也算命中；默认参数行为不变。

运行：cd /d F:\coding\guanjia && .venv\Scripts\python.exe tests\test_wake_alt.py
"""
from guanjia.wake import WakeMatcher, decide, rescue_wake

M = WakeMatcher(["管家"])
KW = dict(threshold=0.45, matcher=M, wake_required=True, min_chars=2, in_window=False)


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def d(text, alt=None, score=0.1, **over):
    kw = {**KW, **over}
    return decide(text, score, alt_text=alt, **kw) if alt is not None else decide(text, score, **kw)


# ---------- 1. 默认行为不变 ----------
for t in ("关家，打开记事本", "管家", "打开记事本", "嗯", "家让你干的活", "你好管家"):
    a, b = decide(t, 0.1, **KW), decide(t, 0.1, alt_text=None, **KW)
    check(a == b, f"alt_text=None identical to old behaviour: {t!r} -> {a.verdict}")
check(decide("家让你干的活", 0.1, **KW).verdict == "rejected", "without alt: lost first char = no wake (the bug)")
check(d("家让你干的活", "").verdict == "rejected", "empty alt = no alt")

# ---------- 2. 救回来：最终丢了「管」，流式听对了 ----------
r = d("家让你干的活你干成怎么样了呀", "管家让你干的活你干得怎么样了呀")
check(r.verdict == "accept" and r.text == "让你干的活你干成怎么样了呀" and r.wake == "管家",
      f"lost head char: accept, final text kept, prefix '家' dropped -> {r.text!r}")
check("流式" in r.reason, "reason says it came from the alt text")
r = d("人家帮我把这个 H A L 的这个音响的驱动和我打", "管家帮我把这个HAL的这个音响的驱动给我打开")
check(r.verdict == "accept" and r.text == "帮我把这个 H A L 的这个音响的驱动和我打", f"'人家' -> rescued, final text used: {r.text!r}")
r = d("管他让你干的活你干什么样的呀", "管家让你干的活儿你干怎么样的呀")
check(r.verdict == "accept" and r.text == "让你干的活你干什么样的呀", f"'管他' -> rescued: {r.text!r}")
r = d("家", "管家")
check(r.verdict == "wake_only" and r.wake == "管家", f"lone '家' + alt 管家 -> wake_only (was filler-ignored): {r.verdict}")
r = d("管他", "管家")
check(r.verdict == "wake_only", f"lone '管他' + alt 管家 -> wake_only: {r.verdict}")
r = d("家，打开记事本", "管家打开记事本")
check(r.verdict == "accept" and r.text == "打开记事本", "punctuation after the lost-head remnant is trimmed")
r = d("人家让你干的活", "管家让你干的活", score=0.0)
check(r.verdict == "accept", "low speaker score never blocks a wake-word hit (same as before)")
r = d("天早上七点半叫我起床", "明天早上七点半叫我起床")
check(r.verdict == "rejected", "alt without wake word changes nothing")

# ---------- 3. 保守：不该放行的 ----------
check(d("打开记事本", "打开记事本管家").verdict == "rejected", "alt hit at the tail (not head) is not trusted")
check(d("帮我开灯", "我今天听见管家说帮我开灯").verdict == "rejected", "alt hit in the middle is not trusted")
check(d("你们好", "你好管家").verdict == "rejected", "alt hit after a real prefix is not trusted")
check(d("人家让你干的活你干什么样的呀", "管他让你干的活儿你干怎么样的呀").verdict == "rejected", "alt also wrong -> no wake")

# ---------- 4. 最终文本自己命中时，备选文本不起作用 ----------
a = d("关家，打开记事本", "管家打开")
check(a.verdict == "accept" and a.text == "打开记事本" and a.wake == "关家", "final's own hit wins; alt ignored")

# ---------- 5. 最终没有前缀残留、只是流式多了唤醒词 ----------
r = d("帮我开灯", "管家帮我开灯")
check(r.verdict == "accept" and r.text == "帮我开灯", f"nothing to drop from final: {r.text!r}")

# ---------- 6. 其他开关照旧 ----------
r = d("家让你干的活", "管家让你干的活", wake_required=False)
check(r.verdict == "accept", "wake_required=False: still accept")
r = d("好", "好", in_window=True, score=0.9)
check(r.verdict == "accept" and r.reason == "免喊窗口内", "in_window short answer unaffected")

# ---------- 7. 纯函数 rescue_wake ----------
check(rescue_wake("打开记事本", None, M) is None and rescue_wake("打开记事本", "", M) is None, "rescue_wake: no alt -> None")
rest, hit = rescue_wake("家让你干的活", "管家让你干的活", M)
check(rest == "让你干的活" and hit.heard == "管家", "rescue_wake returns (rest, hit)")
check(rescue_wake("打开记事本", "打开记事本", M) is None, "rescue_wake: no hit in alt -> None")
print("ALL PASS")
