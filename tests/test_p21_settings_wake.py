"""P2.1 设置与唤醒词用例：设置校验/落盘/手工改文件热加载、拼音容错匹配、碎音过滤、去留判定。不碰音频和网络。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p21_settings_wake.py
"""
import json
import os
import tempfile
import time
from pathlib import Path

from guanjia.settings import Settings, SettingsStore, from_dict, to_dict
from guanjia.wake import WakeMatcher, decide, is_filler


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def raises(fn) -> str:
    try:
        fn()
    except ValueError as e:
        return str(e)
    return ""


# ---------- 1. 设置 ----------
d = Settings()
check(d.wake_words == ("管家",) and d.follow_up_sec == 20.0 and d.spk_threshold == 0.45, "defaults: 管家 / 20s / 0.45")
s = from_dict({"wake_words": "小管， 管家,小管", "follow_up_sec": "30", "spk_threshold": 0.5})
check(s.wake_words == ("小管", "管家") and s.follow_up_sec == 30.0 and s.spk_threshold == 0.5, "comma string, dedupe, numbers")
check("唤醒词" in raises(lambda: from_dict({"wake_words": []})), "empty wake words refused")
check("唤醒词" in raises(lambda: from_dict({"wake_words": ["一二三四五六七八九"]})), "wake word too long refused")
check("spk_threshold" in raises(lambda: from_dict({"spk_threshold": 1.5})), "threshold out of range refused")
check("不认识" in raises(lambda: from_dict({"evil": 1})), "unknown key refused")
check("true" in raises(lambda: from_dict({"wake_required": "no"})), "bool must be bool")
check(to_dict(d)["wake_words"] == ["管家"], "to_dict gives JSON-friendly list")

tmp = Path(tempfile.mkdtemp(prefix="gj_p21_")) / "voice-settings.json"
store = SettingsStore(tmp)
seen = []
store.subscribe(seen.append)
check(store.current == Settings() and not tmp.exists(), "missing file -> defaults, nothing written")
store.update({"wake_words": ["小管"], "follow_up_sec": 15})
check(json.loads(tmp.read_text(encoding="utf-8"))["wake_words"] == ["小管"], "update writes the file")
check(len(seen) == 1 and seen[0].follow_up_sec == 15.0, "update notifies listeners")
check(raises(lambda: store.update({"min_chars": 99})) and store.current.min_chars == 2, "bad update keeps current value")
time.sleep(0.05)
tmp.write_text(json.dumps({"wake_words": ["老管"], "spk_threshold": 0.4}, ensure_ascii=False), encoding="utf-8")
os.utime(tmp, (time.time() + 5, time.time() + 5))
check(store.maybe_reload() and store.current.wake_words == ("老管",) and store.current.spk_threshold == 0.4,
      "hand-edited file picked up by maybe_reload")
check(len(seen) == 2, "hand edit notifies listeners")
check(not store.maybe_reload(), "unchanged file is not reloaded again")
tmp.write_text("{broken", encoding="utf-8")
os.utime(tmp, (time.time() + 10, time.time() + 10))
check(not store.maybe_reload() and store.current.wake_words == ("老管",), "broken file keeps current settings")
check(SettingsStore(None).update({"follow_up_sec": 5}).follow_up_sec == 5.0, "in-memory store works without a file")

# ---------- 2. 唤醒词匹配 ----------
m = WakeMatcher(("管家",))
for heard in ("管家，打开记事本", "关家打开记事本", "管加，开灯", "打开记事本，管家", "光价帮我查天气"):
    check(m.find(heard) is not None, f"wake hit: {heard}")
for miss in ("打开记事本", "关键是要打开", "管理一下", "周老师点的不一样吗", "玩还绿吧", "的转"):
    check(m.find(miss) is None, f"no wake: {miss}")
hit = m.find("嗯，关家，打开记事本")
check(hit.heard == "关家" and hit.word == "管家", "hit reports how it was heard")
check(m.strip("管家，打开记事本") == ("打开记事本", m.find("管家，打开记事本")), "strip removes wake word and comma")
check(m.strip("打开记事本，管家啊")[0] == "打开记事本", "strip trailing wake word and particle")
check(m.strip("管家管家")[0] == "", "repeated wake word leaves nothing")
m2 = WakeMatcher(("小乐",))
check(m2.find("小了，开灯") is not None, "polyphone in heard text: 了 also reads le")
for miss in ("关节疼怎么办", "光顾一下", "这广告真烦", "关街那边"):
    check(m.find(miss) is None, f"rare readings of the wake word do not hit: {miss}")
m3 = WakeMatcher(("贾维斯同学",))
check(m3.find("假为斯同雪开灯") is not None and m3.find("今天天气") is None, "4+ chars allow one wrong char")
m4 = WakeMatcher(("Jarvis",))
check(m4.find("hey jarvis 开灯") is not None, "latin wake word case-insensitive")

# ---------- 3. 碎音 ----------
for junk in ("唉", "呃", "嗯", "嗯嗯", "啊？", "哦。", ""):
    check(is_filler(junk, 2), f"filler: {junk!r}")
for ok in ("的转", "打开记事本", "开灯"):
    check(not is_filler(ok, 2), f"not filler: {ok}")
check(is_filler("好", 2) and not is_filler("好", 2, in_window=True), "single-char answer only allowed in window")

# ---------- 4. 去留判定 ----------
def dd(text, score=0.6, window=False, required=True):
    return decide(text, score, threshold=0.45, matcher=m, wake_required=required, min_chars=2, in_window=window)


r = dd("唉", 0.41)
check(r.verdict == "ignored" and r.stage == "filler", "single grunt ignored")
r = dd("管家打开记事本", 0.30)
check(r.verdict == "accept" and r.text == "打开记事本", "wake word + low score accepted")
r = dd("管家", 0.30)
check(r.verdict == "wake_only" and r.text == "", "wake word alone + low score -> wake_only")
r = dd("开灯", 0.30, window=True)
check(r.verdict == "accept" and r.stage == "" and r.reason == "免喊窗口内" and r.text == "开灯",
      "in window, no wake, low score: accepted (voiceprint no longer blocks)")
r = dd("开灯", 0.30, required=False)
check(r.verdict == "accept" and r.reason == "不需要唤醒词" and r.text == "开灯",
      "wake not required, no wake word, low score: accepted")
r = dd("玩还绿吧", 0.6)
check(r.verdict == "rejected" and r.stage == "wake" and r.reason == "没有唤醒词",
      "outside window, no wake, high score rejected by wake")
r = dd("开灯", 0.80, window=True)
check(r.verdict == "accept" and r.reason == "免喊窗口内", "in window, high score accepts")
r = dd("打开记事本", None, required=False)
check(r.verdict == "accept", "score None accepted too")
r = dd("关家，打开记事本")
check(r.verdict == "accept" and r.text == "打开记事本" and r.wake == "关家", "wake accepted, wake word stripped")
r = dd("管家")
check(r.verdict == "wake_only" and r.text == "", "wake word alone -> wake_only")
r = dd("管家，嗯")
check(r.verdict == "wake_only", "wake word plus grunt -> wake_only")
r = dd("打开记事本", window=True)
check(r.verdict == "accept" and r.text == "打开记事本" and r.reason == "免喊窗口内", "in window no wake needed")
r = dd("好", window=True)
check(r.verdict == "accept" and r.text == "好", "short answer accepted in window")
r = dd("打开记事本", required=False)
check(r.verdict == "accept", "wake_required=False behaves like P2")
r = dd("管家打开记事本", None)
check(r.verdict == "accept", "score None (inject) skips voiceprint")

print("ALL PASS test_p21_settings_wake")
