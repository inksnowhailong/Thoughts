"""口语整形：去 Markdown / 表情、只念前 N 句、超长句在逗号处切短（切不开就整句保留）、流式切句路径上边生成边整形、/say 不整形、
设置项 speech_max_sentences（默认 2，0 = 不整形）、整形事件与「漏念」口径。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_speech_shape.py
"""
import numpy as np

from guanjia.events import EventHub, turn_summary
from guanjia.settings import Settings, from_dict
from guanjia.speech_shape import SHAPED_PREFIX, SpeechShaper, split_long, to_speech
from guanjia.textseg import SentenceSplitter
from guanjia.turns import SayItem, TurnRunner


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


# ---------- 1. 去 Markdown / 表情 ----------
check(to_speech("**现在需要你点头：** 我查一下") == "现在需要你点头： 我查一下", "bold removed")
check(to_speech("# 小标题") == "小标题" and to_speech("- 第一项") == "第一项" and to_speech("2. 第二项") == "第二项", "heading and list markers removed")
check(to_speech("看这个 [官方页面](https://www.edifier.com/a?b=1) 就行") == "看这个 官方页面 就行", "link keeps only its text")
check(to_speech("![图](http://x/y.png)好的") == "好的", "image dropped")
check(to_speech("用 `exec` 跑一下 😀") == "用 exec 跑一下", "backticks and emoji removed")
check(to_speech("音响（Harman Kardon）的驱动") == "音响（Harman Kardon）的驱动", "English side note in parentheses kept")
check(to_speech("---") == "" and to_speech("😀") == "" and to_speech("NO_REPLY") == "", "nothing speakable -> empty")

# ---------- 2. 超长句切短 ----------
check(split_long("今天天气不错。") == ["今天天气不错。"], "short sentence untouched")
long_ = "第一部分有十来个字，第二部分也有十来个字，第三部分还是十来个字，最后一部分收个尾。"
parts = split_long(long_)
check(len(parts) >= 2 and all(len(p) <= 30 for p in parts) and "".join(parts) == long_, f"long sentence cut at commas, nothing lost: {parts}")
no_comma = "这是一句完全没有任何逗号也没有顿号的特别长的句子一直说一直说一直说不停"
check(split_long(no_comma) == [no_comma], "no comma within reach -> keep the whole sentence (no mid-word cut)")
late_comma = "这是一句前面三十多个字都没有逗号的句子一直说一直说一直说一直说一直说，然后才有逗号再收尾。"
check(split_long(late_comma)[0].endswith("，") and "".join(split_long(late_comma)) == late_comma, "first comma beyond 30: keep up to it, still no mid-word cut")
check(split_long("甲、乙、丙、丁、戊、己、庚、辛、壬、癸、子、丑、寅、卯、辰、巳、午、未、申、酉、戌、亥。") != [], "enumeration with 顿号 is cut")
check(all(len(p) <= 30 for p in split_long("甲、乙、丙、丁、戊、己、庚、辛、壬、癸、子、丑、寅、卯、辰、巳、午、未、申、酉、戌、亥。")), "each chunk of the enumeration within 30")

# ---------- 3. SpeechShaper：只念前 N 句 ----------
sh = SpeechShaper(2)
out = [sh.shape(c) for c in ["你好，", "我是管家。", "今天天气不错，", "适合出门。", "第三句。", "第四句，", "还有尾巴"]]
check([o[0] for o in out[:4]] == [["你好，"], ["我是管家。"], ["今天天气不错，"], ["适合出门。"]], "first two sentences (incl. comma-cut chunks) are spoken")
check(out[4] == ([], "第三句。") and out[5] == ([], "第四句，") and out[6] == ([], "还有尾巴"), "third sentence onwards omitted and returned as text")
st = sh.stats()
check(st == {"sentences": 4, "chars": 32, "spoken_sentences": 2, "omitted": 3, "split": 0, "limit": 2}, f"stats: {st}")

sh = SpeechShaper(2)
check(sh.shape("好的。")[0] == ["好的。"] and sh.shape("没问题。")[0] == ["没问题。"] and sh.stats() is None, "a reply within the limit is untouched (stats None)")

sh = SpeechShaper(2)
for c in ["- 第一项", "- 第二项", "- 第三项"]:
    sh.shape(c, ends=True)  # SentenceSplitter 切出的块已去掉末尾换行，句末由 ends 参数告知
check(sh.shape("- 第四项", ends=True) == ([], "第四项") and sh.stats()["spoken_sentences"] == 2, "markdown list: only two items are read")

sh = SpeechShaper(2)
check(sh.shape("---") == ([], "") and sh.shape("😀") == ([], "") and sh.shape("好。")[0] == ["好。"] and sh.sentences == 1, "empty-after-clean chunks are neither read nor counted")

sh = SpeechShaper(0)
check(all(sh.shape(f"第{i}句。")[0] for i in range(6)) and sh.stats() is None, "limit 0 = unlimited")

sh = SpeechShaper(1)
a, _ = sh.shape("先说这一句。")
b = sh.shape("再说那一句。")
check(a and b == ([], "再说那一句。"), "limit 1")

sh = SpeechShaper(2)
pieces, _ = sh.shape("第一部分有十来个字，第二部分也有十来个字，第三部分还是十来个字，最后一部分收个尾。")
check(len(pieces) >= 2 and sh.stats()["split"] == len(pieces) - 1 and sh.stats()["omitted"] == 0, "split long sentence counts as one sentence and is reported")


# ---------- 4. TurnRunner 切句路径：边生成边整形 ----------
class Rec:
    def __init__(self):
        self.synth_calls, self.enq, self.events = [], [], []

    def synth(self, s):
        self.synth_calls.append(s)
        return np.zeros(160, dtype=np.float32), 16000

    def enqueue(self, gen, pcm, sr):
        self.enq.append(gen)

    def on_event(self, name, data):
        self.events.append((name, data))


def runner(rec, factory, stream):
    return TurnRunner(open_stream=lambda text: stream(rec), synth=rec.synth, enqueue=rec.enqueue, current_gen=lambda: 0,
                      first_max_chars=20, first_soft_min=6, on_event=rec.on_event, shaper=factory)


REPLY = ["好的，我先看一下。", "音响的驱动**已经装好**了，", "官方控制软件还在找。", "[官网](http://x.com)的页面是 404，", "我再搜一下。", "有结果再告诉你。"]
rec = Rec()
res = runner(rec, lambda: SpeechShaper(2), lambda r: iter(REPLY)).run_reply("x", 0)
check(res.reply == "".join(REPLY), "reply keeps the complete original text (panel / record / nudge judging)")
check(rec.synth_calls == ["好的，我先看一下。", "音响的驱动已经装好了，官方控制软件还在找。"], f"only the first two sentences go to TTS: {rec.synth_calls}")
check(res.spoken == rec.synth_calls and res.aborted is False and res.error is None, "spoken list matches what was synthesized")
shaped = [x for x in res.unspoken if x.startswith(SHAPED_PREFIX)]
check(len(shaped) == len(res.unspoken) == 3 and shaped[0] == SHAPED_PREFIX + "官网的页面是 404，" and shaped[-1] == SHAPED_PREFIX + "有结果再告诉你。",
      f"omitted chunks recorded in unspoken with the prefix: {res.unspoken}")
ev = [d for n, d in rec.events if n == "shaped"]
check(len(ev) == 1 and ev[0]["omitted"] == 3 and ev[0]["spoken_sentences"] == 2 and ev[0]["sentences"] == 4 and ev[0]["limit"] == 2
      and ev[0]["chars"] > 30, f"one 'shaped' event with counts: {ev}")

# 首字：第一块在流还没读完时就已经送 TTS（不等整段回复）
rec = Rec()
seen = {}


def stream_probe(r):
    yield "好的，我先看一下。"
    seen["synth_before_second_piece"] = list(r.synth_calls)
    yield "第二句。第三句。"


runner(rec, lambda: SpeechShaper(2), stream_probe).run_reply("x", 0)
check(seen["synth_before_second_piece"] == ["好的，我先看一下。"], "first chunks are synthesized while the stream is still running")

# 换行结尾的列表项：SentenceSplitter 切块时去掉了换行，靠 last_end 仍能按「项」数句
rec = Rec()
res = runner(rec, lambda: SpeechShaper(2), lambda r: iter(["要做三件事：\n", "- 先开机\n- 再联网\n- 最后登录\n"])).run_reply("x", 0)
check(rec.synth_calls == ["要做三件事：", "先开机", "再联网"] and [x for x in res.unspoken if x.startswith(SHAPED_PREFIX)] == [SHAPED_PREFIX + "最后登录"],
      f"newline-terminated list items count as sentences: {rec.synth_calls} / {res.unspoken}")

# 没有整形器 / 工厂返回 None（speech_max_sentences=0）：和以前完全一样
rec = Rec()
res = runner(rec, None, lambda r: iter(REPLY)).run_reply("x", 0)
check(len(res.spoken) == 5 and res.unspoken == [] and not [d for n, d in rec.events if n == "shaped"], "no shaper -> everything is read, no shaped event")
rec = Rec()
res = runner(rec, lambda: None, lambda r: iter(REPLY)).run_reply("x", 0)
check(len(res.spoken) == 5 and res.unspoken == [], "factory returning None -> unchanged behaviour")

# /say 不整形
rec = Rec()
res = runner(rec, lambda: SpeechShaper(1), lambda r: iter(())).run_say("第一句。第二句。第三句。", 0)
check(len(res.spoken) == 3 and res.unspoken == [], "/say is never shaped")
check(isinstance(SayItem("x"), SayItem), "SayItem unchanged")

# 回复里一句都不用省：不记 shaped
rec = Rec()
runner(rec, lambda: SpeechShaper(2), lambda r: iter(["好的。", "马上。"])).run_reply("x", 0)
check(not [d for n, d in rec.events if n == "shaped"], "nothing shaped -> no event")

# 被打断：整形不改变打断语义
rec = Rec()
gen_now = [0]
r2 = TurnRunner(open_stream=lambda text: iter(REPLY), synth=rec.synth, enqueue=rec.enqueue, current_gen=lambda: gen_now[0], first_max_chars=20,
                first_soft_min=6, on_event=rec.on_event, shaper=lambda: SpeechShaper(2))
res = r2.run_reply("x", 1)  # 播放代次已变 → 视为被打断
check(res.aborted and res.spoken == [] and res.reply == "".join(REPLY), "interrupted turn: still aborted, full reply still collected")

# ---------- 5. 记录口径：整形省略不算「漏念」 ----------
hub = EventHub()
uid = hub.new_utterance("mic", text="x")
hub.finish(uid, verdict="accept", reply="全文", spoken=["a"], unspoken=[SHAPED_PREFIX + "第三句。", "真没念出来的"])
s = turn_summary(hub.record(uid))
check(s["unspoken"] == 1 and s["shaped"] == 2 - 1, f"turn summary separates real unspoken from shaped: {s['unspoken']}/{s['shaped']}")
uid2 = hub.new_utterance("mic", text="y")
hub.finish(uid2, verdict="accept", reply="全文", spoken=["a"], unspoken=[SHAPED_PREFIX + "第三句。"])
s2 = turn_summary(hub.record(uid2))
check(s2["unspoken"] == 0 and s2["shaped"] == 1, "only-shaped turn has unspoken 0 (no warn level, no diagnosis noise)")
check(hub.record(uid2)["unspoken"] == [SHAPED_PREFIX + "第三句。"], "the panel record still lists the omitted text")

# ---------- 6. 设置项 ----------
check(Settings().speech_max_sentences == 2, "default 2")
check(from_dict({"speech_max_sentences": 0}).speech_max_sentences == 0 and from_dict({"speech_max_sentences": 3}).speech_max_sentences == 3, "0 and 3 accepted")
for bad in (-1, 11, "x"):
    try:
        from_dict({"speech_max_sentences": bad})
        check(False, f"{bad!r} should be refused")
    except ValueError:
        pass
check(True, "out of range / non-numeric refused")
check(isinstance(from_dict({"speech_max_sentences": "3"}).speech_max_sentences, int), "stored as int")

# 句子切分器没被动过：首句仍按 6 字逗号 / 20 字硬上限出声
sp = SentenceSplitter(first_hard_max_chars=20, first_soft_min_chars=6)
check(list(sp.push("好的好的好的，我先看一下。")) == ["好的好的好的，", "我先看一下。"] and sp.last_end is True, "SentenceSplitter behaviour unchanged (+ last_end)")
sp = SentenceSplitter()
check(list(sp.push("第一项\n")) == ["第一项"] and sp.last_end is True and list(sp.flush()) == [] , "newline cut reports last_end")
sp = SentenceSplitter(first_soft_min_chars=2)
check(list(sp.push("好的，")) == ["好的，"] and sp.last_end is False, "comma cut is not a sentence end")

print("ALL PASS test_speech_shape")
