"""logq 日志查询：最近几轮、一轮时间线、筛选、关键字（含 base64）、长字段、行数/字数上限、多进程合并、UTF-8 输出、旧记录兜底。
夹具：临时目录里造 events-voice-<日期>.jsonl / events-supervisor-<日期>.jsonl / 旧 utterances.jsonl，全部走 logq.main([...])。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_logq.py
"""
import base64
import contextlib
import io
import json
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from guanjia import logq


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


NOW = time.time()
BASE = NOW - 1800  # 半小时前开始；各事件按自己的日期落到对应文件，跨零点也成立
U1, U2, U3 = "u130001-001", "u130002-002", "u130003-003"


def ev(t, comp, ev_, msg="", level="info", uid=None, **data):
    e = {"ts": round(BASE + t, 3), "comp": comp, "level": level, "ev": ev_}
    if uid:
        e["uid"] = uid
    if msg:
        e["msg"] = msg
    if data:
        e["data"] = data
    return e


def write_events(d: Path, proc: str, events: list[dict]) -> None:
    by_day = defaultdict(list)
    for e in events:
        by_day[datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d")].append(e)
    for day, es in by_day.items():
        (d / f"events-{proc}-{day}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in es) + "\n", encoding="utf-8")


def gw_event(t, uid, tools, stop="stop", texts=None, **extra):
    return ev(t, "gateway", "gateway.run", "m stop=" + stop, uid=uid, run_id="chatcmpl_aaaaaaaaaaaaaaaaaaaa", provider="p",
              model="p/DeepSeek-V4-Flash", stop=stop, final_status="success", usage={"input": 100, "output": 20, "cacheRead": 50},
              tools=tools, texts=texts or [], **extra)


def build(d: Path) -> None:
    voice = [
        ev(0, "voice", "utt.new", "打开音响", uid=U1, source="mic", score=0.61),
        ev(0.1, "vad", "vad.done", "2.0 秒", uid=U1),
        ev(0.2, "asr", "asr.done", "打开音响", uid=U1),
        ev(0.3, "speaker", "speaker.done", "0.61 ≥ 0.40", uid=U1),
        ev(0.5, "tts", "tts.blocked", "TTS：超时", level="warn", uid=U1),
        ev(1.0, "voice", "turn.end", "accept 「打开音响」", uid=U1, source="mic", verdict="accept", text="打开音响",
           reply="我先用只读方式找一下", spoken=1, unspoken=1, timings={"first_sound_ms": 900}),
        gw_event(1.2, U1, []),
        ev(5, "voice", "utt.new", "现在几点", uid=U2, source="mic"),
        ev(5.2, "play", "play.done", "念完 1 句", uid=U2),
        ev(6, "voice", "turn.end", "accept 「现在几点」", uid=U2, source="mic", verdict="accept", text="现在几点", reply="三点整",
           spoken=1, unspoken=0, timings={"first_sound_ms": 700}),
        ev(7, "panel", "user_flag", "没打开", level="warn", uid=U2, text="现在几点", reply="三点整"),
        ev(10, "voice", "utt.new", "嗯", uid=U3, source="mic"),
        ev(10.5, "voice", "turn.end", "ignored 「嗯」", uid=U3, source="mic", verdict="ignored", text="嗯", reply="", spoken=0, unspoken=0),
        ev(12, "log", "log", "很长的一行", level="warn", big="长" * 2000),
        ev(13, "enroll", "enroll.seg", "声音太小", level="warn", ok=False, group="near", zero_ratio=0.7),
        ev(14, "enroll", "enroll.seg", "收下", ok=True, group="near", zero_ratio=0.1),
        ev(15, "tts", "tts.done", "ok", uid=U2, ms=300),
    ]
    sup = [ev(2, "supervisor", "sup.start", "启动", pid=111), ev(8, "supervisor", "mutex_busy", "有人占着", level="warn", holder_pid=222)]
    write_events(d, "voice", voice)
    write_events(d, "supervisor", sup)


def run(d: Path, *args: str) -> tuple[str, str]:
    """跑 logq.main，返回 (输出文件内容, stdout)。"""
    f = d / "out.txt"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        logq.main([*args, "--log-dir", str(d), "--legacy-file", str(d / "legacy.jsonl"), "--out", str(f)])
    return f.read_text(encoding="utf-8"), buf.getvalue()


with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    build(d)

    # 1. --recent
    text, _ = run(d, "--recent", "5")
    rows = text.strip().splitlines()
    check(len(rows) == 2 and U1 in rows[0] and U2 in rows[1], "--recent 只列 accept，按时间顺序")
    check("stop=stop tools=0" in rows[0] and "DeepSeek-V4-Flash" in rows[0], "有网关段的那行带 model/stop/tools")
    check("⚑" in rows[1] and "漏念=1" in rows[0] and "首音=900ms" in rows[0], "被标记的带旗标，漏念/首音出现")
    text, _ = run(d, "--recent", "5", "--all")
    check(len(text.strip().splitlines()) == 3, "--all 把被忽略的也列出来")
    text, _ = run(d)
    check(U1 in text, "不带参数等价于最近 10 轮")

    # 2. --turn
    text, _ = run(d, "--turn", U1)
    rows = text.splitlines()
    check(rows[0].startswith(f"== {U1}") and rows[1].startswith("+     0ms"), "--turn 首行是标题，事件行以 +0ms 起")
    check(U2 not in text and "tts.blocked" in text and "-- 回复：我先用只读方式找一下" in text, "--turn 不含别的轮、含回复")
    check("!! 有 1 句没念出来" in text, "--turn 末尾有诊断行")
    text, _ = run(d, "--turn", "u000000-999")
    check("没找到" in text, "找不到的 uid 给出提示")

    # 3. 筛选
    text, _ = run(d, "--comp", "tts", "--level", "warn")
    check(text.count("\n") == 1 and "tts.blocked" in text, "--comp tts --level warn 只出 tts 的 warn")
    text, _ = run(d, "--ev", "enroll.*")
    check(text.count("enroll.seg") == 2 and "tts" not in text, "--ev enroll.* 只出 enroll")
    t1, _ = run(d, "-k", "打开音响")
    t2, _ = run(d, "--kb64", base64.b64encode("打开音响".encode()).decode())
    check(t1 == t2 and "utt.new" in t1 and "现在几点" not in t1, "-k 与 --kb64 结果相同")
    text, _ = run(d, "--uid", U2)
    check(U1 not in text and text.count("\n") == 5, "--uid 只出该轮的事件")
    text, _ = run(d, "--since", "40m", "--comp", "enroll")
    check(text.count("enroll.seg") == 2, "--since 40m 能解析并筛出窗口内事件")
    text, _ = run(d, "--since", "10m", "--comp", "enroll")
    check("enroll.seg" not in text, "--since 10m 之前的事件被排除")

    # 4. 长字段
    text, _ = run(d, "--comp", "log", "--level", "warn")
    row = text.strip().splitlines()[0]
    check(len(row) <= logq.LINE_WIDTH and "…" in row, "默认输出该行 ≤160 字且含 …")
    text, _ = run(d, "--comp", "log", "--level", "warn", "--detail", "--max-chars", "20000")
    check("长" * 2000 in text, "--detail 展开完整 2000 字")

    # 5. 上限
    text, _ = run(d, "--level", "info", "--comp", "voice,tts,enroll,play,panel,log,gateway,supervisor", "--max-lines", "3")
    check("省略" in text or "还有" in text, "--max-lines 3 有省略提示")
    text, _ = run(d, "--turn", U1, "--detail", "--max-chars", "200")
    body = [ln for ln in text.splitlines() if not ln.startswith("!!") and not ln.startswith("…还有")]
    check(sum(len(ln) + 1 for ln in body) <= 260 and "还有" in text and "!! 有 1 句" in text, "--max-chars 200 守住上限，诊断行仍保留")

    # 6. 多进程合并交错
    text, _ = run(d, "--boot")
    check("supervisor" in text and "sup.start" in text and "mutex_busy" in text, "--boot 含看门狗事件")
    check(text.index("sup.start") < text.index("mutex_busy"), "--boot 里看门狗事件按时间排")
    allt, _ = run(d, "--comp", "voice,supervisor", "--level", "info")
    # voice 文件里 t=0/5/10 的 utt.new 与 supervisor 的 t=2/8 交错
    idx = [allt.index("utt.new"), allt.index("sup.start"), allt.rindex("utt.new")]
    check(idx[0] < idx[1] < idx[2] and allt.index("mutex_busy") > allt.index("sup.start"), "两个进程的事件按 ts 交错")

    # 7. UTF-8 输出
    text, stdout = run(d, "--recent", "5")
    check("打开音响" in text and stdout.isascii() and stdout.startswith("written "), "--out 文件是 UTF-8 中文，stdout 一行纯 ASCII")
    raw = (d / "out.txt").read_bytes()
    check(b"\r\n" not in raw, "输出文件用 LF")

    # 8. 旧记录兜底
    (d / "legacy.jsonl").write_text(json.dumps({
        "id": "u090000-001", "t0": 1000.0, "source": "mic", "text": "老的一句", "verdict": "accept", "reason": "ok",
        "stages": {"asr": {"state": "done"}}, "timeline": [{"t": 1000.5, "stage": "asr", "state": "done", "note": "老的一句"}],
        "tools": [], "reply": "老回复", "spoken": ["老回复"], "unspoken": [], "timings": {"first_sound_ms": 500}, "run_id": "r1"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    text, _ = run(d, "--turn", "u090000-001")
    check("[旧记录]" in text and "老回复" in text and '"stages"' not in text and "+   500ms" in text, "新事件里没有 → 旧记录兜底，不含 stages")

    # 9. 标记
    text, _ = run(d, "--flags")
    check(U2 in text and "没打开" in text and U1 not in text, "--flags 列出主人标记")

    # 10. 旧文本日志
    (d / "x.log").write_text("a /health ok\nINFO enroll start\nINFO other\n", encoding="utf-8")
    logq.TEXT_LOGS["voice"] = d / "x.log"
    text, _ = run(d, "--tail", "voice")
    check("enroll start" in text and "/health" not in text, "--tail voice 滤掉健康检查")
    text, _ = run(d, "--tail", "voice", "-k", "other")
    check(text.strip() == "INFO other", "--tail 配合 -k 过滤")

# ---------- B3：网关段、诊断、--runs、--payload、网关日志 ----------
def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    gwdir = d / "gwlogs"
    gwdir.mkdir()
    U_PROMISE, U_BAD, U_FAKE, U_GATED, U_PARENT, U_CHILD = (f"u1400{i:02d}-0{i:02d}" for i in range(1, 7))
    blob_rel = f"{datetime.fromtimestamp(BASE).strftime('%Y-%m-%d')}/{U_PROMISE}-gwctx.txt"
    (d / "blobs" / blob_rel).parent.mkdir(parents=True)
    (d / "blobs" / blob_rel).write_text('{\n "prompt": "[语音] 让你干的活干成怎么样了",\n "messages_total": 64\n}', encoding="utf-8")

    def turn(t, uid, reply, **kw):
        return [ev(t, "voice", "utt.new", "x", uid=uid, source="mic"),
                ev(t + 1, "voice", "turn.end", "accept", uid=uid, source="mic", verdict="accept", text="x", reply=reply,
                   spoken=1, unspoken=0, **kw)]

    ok_tool = {"name": "exec", "ok": True, "ms": 120, "exit": 0, "args": "Get-Date"}
    bad_tool = {"name": "web_fetch", "ok": False, "ms": 5, "exit": None, "args": "https://x", "err": "denied by guard"}
    evs_ = [
        *turn(100, U_PROMISE, "我先用只读方式找一下"), gw_event(102, U_PROMISE, [], blob=blob_rel, texts=["我先用只读方式找一下"],
                                                      phases={"session_started": BASE + 100.1, "prompt_submitted": BASE + 100.2}),
        *turn(110, U_BAD, "没成功"), gw_event(112, U_BAD, [ok_tool, bad_tool], texts=["没成功"]),
        *turn(120, U_FAKE, "这个命令需要审批，已经超时没批下来"), gw_event(122, U_FAKE, [], texts=["需要审批"]),
        *turn(130, U_GATED, "这个命令需要审批，请你确认"), gw_event(132, U_GATED, [], texts=["需要审批"]),
        ev(131, "tool", "tool.gate", "block-need-confirm x", uid=U_GATED, decision="block-need-confirm"),
        *turn(140, U_PARENT, "我先查一下"), gw_event(142, U_PARENT, [], texts=["我先查一下"]),
        ev(143, "nudge", "nudge.check", "promise -> 追问", uid=U_PARENT, decision=True, why="nudge"),
        ev(144, "nudge", "nudge.sent", "只说没做", uid=U_CHILD, parent=U_PARENT, child=U_CHILD, kind="promise"),
        *turn(145, U_CHILD, "查完了", source_ok=True), gw_event(147, U_CHILD, [ok_tool], texts=["查完了"]),
    ]
    write_events(d, "voice", evs_)
    # 网关日志：窗口内 3 行（WARN、含本 run、无关 INFO）+ 窗口外 1 行 WARN
    day = datetime.fromtimestamp(BASE).strftime("%Y-%m-%d")
    glines = []
    for ts, lv, msg in ((BASE + 101, "WARN", "retrying request after 429"), (BASE + 101.5, "INFO", "run chatcmpl_aaaaaaaaaaaaaaaaaaaa ended with stopReason=stop"),
                        (BASE + 101.6, "INFO", "weixin monitor heartbeat"), (BASE + 500, "WARN", "far away warning")):
        glines.append(json.dumps({"time": iso(ts), "_meta": {"logLevelName": lv}, "message": msg}, ensure_ascii=False))
    (gwdir / f"openclaw-{day}.log").write_text("\n".join(glines) + "\n", encoding="utf-8")

    def runb3(*args):
        return run(d, *args, "--gw-log-dir", str(gwdir))

    text, _ = runb3("--turn", U_PROMISE)
    check("!! 模型说了要做" in text and "一次工具调用都没发" in text, "承诺但零工具调用 → 诊断「模型行为问题」")
    check("本轮模型没有发起任何工具调用" in text and "DeepSeek-V4-Flash" in text and "token 入100/出20/缓存50" in text, "网关段：模型、token、零工具调用")
    import re

    check(re.search(r"阶段：session_started=\+(99|100|101)ms prompt_submitted=\+(199|200|201)ms", text) is not None,
          "阶段时间换成相对本轮首个事件的毫秒")
    check("retrying request after 429" in text and "stopReason=stop" in text and "heartbeat" not in text and "far away" not in text,
          "网关日志只带 WARN 与含 run_id 的行，窗口外和无关行不要")
    check(f"blob={blob_rel}" in text, "网关段指出 blob 位置")

    text, _ = runb3("--turn", U_BAD)
    check("✗ web_fetch" in text and "✓ exec" in text and "!! 工具失败 1 次" in text and "denied by guard" in text, "失败工具带叉号和错误，诊断工具失败")

    text, _ = runb3("--turn", U_FAKE)
    check("疑似模型编造" in text, "声称被拦但没有任何拦截记录 → 诊断编造")
    text, _ = runb3("--turn", U_GATED)
    check("疑似模型编造" not in text, "有 tool.gate 记录时不报编造")

    text, _ = runb3("--turn", U_PARENT)
    check(f"追问轮：--turn {U_CHILD}" in text, "被追问的那一轮提示追问轮 uid")
    text, _ = runb3("--turn", U_CHILD)
    check("✓ exec" in text and "模型没有发起任何工具调用" not in text, "追问轮看自己的网关段")

    text, _ = runb3("--recent", "10")
    line = next(ln for ln in text.splitlines() if U_BAD in ln)
    check("tools=2(失败1)" in line, "--recent 里工具失败数显示出来")

    # --payload：blob 优先
    text, _ = runb3("--payload", U_PROMISE)
    check("messages_total" in text and "让你干的活干成怎么样了" in text, "--payload <uid> 读 blob")
    text, _ = runb3("--payload", U_BAD, "--agent-db", str(d / "nope.sqlite"))
    check("没有" in text and "上下文" in text, "没有 blob 也没有 trajectory → 给出提示不抛错")

    # --runs：用临时 trajectory 库
    import sqlite3

    db = d / "agent.sqlite"
    con = sqlite3.connect(db)
    con.execute("create table trajectory_runtime_events (session_id text, seq integer, run_id text, event_json text, created_at integer)")
    t0 = int(BASE * 1000)
    rows = [("session.started", {"messageProvider": "webchat"}), ("context.compiled", {"prompt": "[语音] 现在几点", "messages": [], "tools": []}),
            ("tool.call", {"toolCallId": "c", "name": "exec", "args": {}}),
            ("tool.result", {"toolCallId": "c", "name": "exec", "success": False, "result": {"content": [{"text": "boom"}]}}),
            ("model.completed", {"stopReason": "stop"}), ("trace.artifacts", {"stopReason": "stop", "assistantTexts": ["三点整"]})]
    for rid, off in (("chatcmpl_aaaaaaaaaaaaaaaaaaaa", 0), ("chatcmpl_trajonly00000000000", 1000)):  # 第二个只在 trajectory 里、事件流没有
        for i, (typ, data) in enumerate(rows):
            con.execute("insert into trajectory_runtime_events values ('s', ?, ?, ?, ?)",
                        (off + i, rid, json.dumps({"traceSchema": "openclaw-trajectory", "type": typ, "data": data}, ensure_ascii=False), t0 + i * 10))
    con.commit()
    con.close()
    text, _ = runb3("--runs", "3", "--agent-db", str(db))
    check("stop=stop tools=1(失败1)" in text and "「[语音] 现在几点」→「三点整」" in text and "webchat" in text, "--runs 一行一个 run，含 stop/工具/提示/回复")
    check(f"uid={U_CHILD}" in text, "--runs 标出能对上的语音 uid（同一 run_id 取最后一个事件的 uid；对不上是 -）")
    check("uid=-" in text, "--runs 对不上语音 uid 的 run 标 -")
    text, _ = runb3("--payload", "chatcmpl_trajonly00000000000", "--agent-db", str(db))
    check('"prompt"' in text and "现在几点" in text, "--payload <run_id> 直接查 trajectory")
    text, _ = runb3("--runs", "3", "--agent-db", str(d / "nope.sqlite"))
    check(text.strip() == "", "网关库不存在时 --runs 空输出不抛错")

line = logq.turn_line({"ts": 1_800_000_000.0, "uid": "u1", "data": {"source": "mic", "verdict": "accept", "text": "x", "reply": "y", "shaped": 2, "unspoken": 0}}, None, None)
check("整形省略=2" in line and "漏念" not in line, "turn line shows shaped chunks separately from missed ones")

# 时间解析
now = 1_800_000_000.0
check(logq.parse_time("10m", now) == now - 600 and logq.parse_time("2h", now) == now - 7200, "parse_time 相对时间")
t = datetime.fromtimestamp(logq.parse_time("13:40", now))
check((t.hour, t.minute) == (13, 40) and t.date() == datetime.fromtimestamp(now).date(), "parse_time 当天 13:40")
check(datetime.fromtimestamp(logq.parse_time("2026-10-11 13:40", now)) == datetime(2026, 10, 11, 13, 40), "parse_time 完整日期时间")

print("ALL PASS test_logq")
