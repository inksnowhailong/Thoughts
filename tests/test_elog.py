"""统一事件日志：格式、级别、脱敏、截断、按天分文件、清理、blob、异步不丢、EventHub 派生、LogBridge。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_elog.py
"""
import json
import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path

from guanjia import elog
from guanjia.events import EventHub


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def lines(d):
    return [json.loads(ln) for f in sorted(Path(d).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]


with tempfile.TemporaryDirectory() as d:
    now = [datetime(2026, 10, 11, 13, 0).timestamp()]
    log = elog.EventLog("t", Path(d), sync=True, clock=lambda: now[0])
    log.emit("brain", "brain.run", "首字", uid="u1", run_id="chatcmpl_x", none_field=None)
    log.emit("asr", "asr.partial", level="debug")
    log.emit("tool", "tool.audit", "Authorization: Bearer abcdefghijklmn sk-ABCDEFGH12345", cmd="x" * 1000)
    got = lines(d)
    check(len(got) == 2, "debug 默认不落盘")
    check(set(got[0]) == {"ts", "comp", "level", "ev", "uid", "msg", "data"} and "none_field" not in got[0]["data"], "字段齐全、None 不记")
    check("abcdefghijklmn" not in json.dumps(got[1], ensure_ascii=False) and "sk-***" in got[1]["msg"], "脱敏")
    check(len(got[1]["data"]["cmd"]) < 420 and "…(+" in got[1]["data"]["cmd"], "长字符串截断")
    check(got[0]["ts"] == round(now[0], 3), "ts 用注入的时钟、3 位小数")
    now[0] += 86400
    log.emit("voice", "voice.start")
    check(len(list(Path(d).glob("events-t-*.jsonl"))) == 2, "跨天换文件")
    old = Path(d) / "events-t-2026-09-01.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    now[0] += 86400
    log.emit("voice", "x")
    check(not old.exists(), "超过 14 天的文件被清理")
    rel = log.blob("u1", "gwctx", {"messages": ["你好" * 10]})
    check(rel and (Path(d) / "blobs" / rel).read_text(encoding="utf-8").count("你好") == 10, "blob 落盘")
    rel2 = log.blob("u2", "big", "Bearer abcdefghijklmnop " + "字" * 200_000)
    txt = (Path(d) / "blobs" / rel2).read_text(encoding="utf-8")
    check("abcdefghijklmnop" not in txt and "截断" in txt and len(txt.encode("utf-8")) <= 270_000, "blob 脱敏且截到 256KB 内")
    # blob 清理：7 天前的目录删掉；总量超限删最旧
    olddir = Path(d) / "blobs" / "2026-09-01"
    olddir.mkdir(parents=True)
    (olddir / "u-x.txt").write_text("old", encoding="utf-8")
    now[0] += 86400
    log.emit("voice", "y")
    check(not olddir.exists(), "超过 7 天的 blob 目录被清理")
    elog.BLOB_MAX_TOTAL, keep = 1, elog.BLOB_MAX_TOTAL
    try:
        now[0] += 86400
        log.emit("voice", "z")
        check(not any((Path(d) / "blobs").rglob("*.txt")), "blob 总量超限删最旧")
    finally:
        elog.BLOB_MAX_TOTAL = keep
    log.close()

with tempfile.TemporaryDirectory() as d:
    log = elog.EventLog("a", Path(d))
    for i in range(2000):
        log.emit("play", "play.tick", n=i)
    log.close()
    check(len(lines(d)) == 2000 and log.dropped == 0, "异步写 2000 条不丢")

bad = elog.EventLog("b", Path(__file__), sync=True)  # 目录其实是个文件：写不进
check(bad.emit("voice", "x") is not None and bad.dropped == 1, "写失败不抛异常，只计数")

fast_dir = tempfile.mkdtemp()
t0 = time.perf_counter()
fast = elog.EventLog("f", Path(fast_dir))
for _ in range(10000):
    fast.emit("tts", "tts.done", "句子", uid="u1", ms=123)
check((time.perf_counter() - t0) / 10000 < 50e-6, "emit 平均 < 50 微秒")
fast.close()

# 分片：单文件超限开 .1
with tempfile.TemporaryDirectory() as d:
    elog.PART_MAX_BYTES, keep = 300, elog.PART_MAX_BYTES
    try:
        log = elog.EventLog("p", Path(d), sync=True)
        for i in range(10):
            log.emit("voice", "x", "y" * 50, n=i)
        log.close()
    finally:
        elog.PART_MAX_BYTES = keep
    check(len(list(Path(d).glob("events-p-*.jsonl"))) >= 2 and len(lines(d)) == 10, "单文件超限开分片，事件不丢")

# EventHub 派生
with tempfile.TemporaryDirectory() as d:
    log = elog.EventLog("h", Path(d), sync=True)
    hub = EventHub(elog=log)
    uid = hub.new_utterance("mic", text="打开音响")
    hub.stage(uid, "tts", "blocked", "TTS：超时")
    hub.stage(None, "vad", "active")
    hub.stage(uid, "speaker", "blocked", "声纹太低")
    hub.finish(uid, verdict="accept", reply="我先查一下", spoken=["我先查一下"], unspoken=["x"])
    evs = [e["ev"] for e in lines(d)]
    check(evs == ["utt.new", "tts.blocked", "speaker.blocked", "turn.end"], f"派生事件顺序（uid=None 的实时状态不落盘）：{evs}")
    got = lines(d)
    check(got[1]["level"] == "warn" and got[1]["comp"] == "tts", "tts blocked -> warn")
    check(got[2]["level"] == "info", "声纹拦下是正常的，不算 warn")
    end = got[-1]
    check(end["level"] == "warn" and end["data"]["unspoken"] == 1 and "timeline" not in end["data"] and "stages" not in end["data"],
          "turn.end 是摘要不是全量")
    check(end["uid"] == uid and "我先查一下" in end["msg"] and end["data"]["verdict"] == "accept", "turn.end 带 uid 与人话摘要")
    uid2 = hub.new_utterance("mic", text="嗯")
    hub.finish(uid2, verdict="ignored", reason="太短", error="网关：boom")
    check(lines(d)[-1]["level"] == "error", "有 error -> turn.end 为 error")
    hub.finish(None)
    check(hub.record(uid)["done"], "finish 仍照旧工作")
    log.close()

# 没有 elog 的 hub 照旧工作（半构造的守护进程）
plain = EventHub()
u = plain.new_utterance("mic", text="x")
plain.stage(u, "asr", "done")
plain.finish(u, verdict="accept")
check(plain.record(u)["verdict"] == "accept", "不带 elog 的 EventHub 不受影响")

# tap 与 LogBridge
with tempfile.TemporaryDirectory() as d:
    log = elog.EventLog("l", Path(d), sync=True)
    seen = []
    log.add_tap(seen.append)
    lg = logging.getLogger("guanjia.test_elog")
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    lg.addHandler(elog.LogBridge(log))
    lg.info("普通信息不进事件流")
    lg.warning("磁盘快满了 token=abcdefghijkl")
    try:
        raise RuntimeError("炸了")
    except RuntimeError:
        lg.exception("前端出错")
    got = lines(d)
    check(len(got) == 2 and got[0]["ev"] == "log" and got[0]["level"] == "warn" and "abcdefghijkl" not in got[0]["msg"],
          "LogBridge 只收 WARNING 以上并脱敏")
    check(got[1]["level"] == "error" and "RuntimeError" in got[1]["data"]["exc"], "异常带堆栈尾")
    check(len(seen) == 2, "tap 收到每条落盘的事件")
    log.close()
    log.close()

# 模块级入口：未初始化是空实现，初始化后可用
check(elog.emit("voice", "x") is None and elog.blob("u", "n", "t") is None, "未初始化时 emit/blob 是空操作")
with tempfile.TemporaryDirectory() as d:
    e = elog.init("m", log_dir=Path(d), sync=True)
    check(elog.get() is e and elog.emit("voice", "voice.start", pid=1) is not None and lines(d)[0]["data"]["pid"] == 1, "init 后模块级 emit 可用")
    e.close()
    e.close()
    elog._default = elog.NullLog()

print("ALL PASS test_elog")
