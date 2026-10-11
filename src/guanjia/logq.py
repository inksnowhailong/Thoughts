"""日志查询：python -m guanjia.logq [选项]。默认紧凑（一事件一行、截断长字段、总量有上限），--detail 才展开。

数据源（全部只读）：
    tmp\\logs\\events-*.jsonl        新事件流（各进程一份，合并后按 ts 排序）
    tmp\\utterances.jsonl            旧记录（--turn 找不到新事件时兜底）
    网关 trajectory（只读）          --runs / --payload 兜底；--turn 的网关段来自 gateway.run 事件（语音进程一轮结束时摘好存下）
    %TEMP%\\openclaw\\openclaw-日期.log  --turn 附本轮时间窗内的 WARN 以上与本 run 相关的网关日志行
远程用法：scripts\\logq.cmd 会加 --out tmp\\logq-out.txt，再 scp 回来读，绕开 ssh 的 GBK 乱码。
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from guanjia import runtrace
from guanjia.events import redact

try:
    from guanjia.turns import claims_blocked, has_promise
except Exception:  # noqa: BLE001  logq 在没有 numpy 的环境（比如拷到别处分析）也要能跑：退回最小词表
    def has_promise(reply: str) -> bool:
        return any(w in reply for w in ("我先", "我看看", "我查一下", "稍等", "马上", "这就", "让我", "我去", "我试试"))

    def claims_blocked(reply: str) -> bool:
        return any(w in reply for w in ("需要审批", "审批超时", "没批下来", "被拦", "需要放行"))

TOOLISH = re.compile(r"<\|?tool|tool_call|function_call|\"name\"\s*:\s*\"\w+\"\s*,\s*\"arguments\"")

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "tmp" / "logs"
LEGACY_RECORDS = ROOT / "tmp" / "utterances.jsonl"
TEXT_LOGS = {"voice": ROOT / "tmp" / "voice.log", "stderr": ROOT / "tmp" / "voice-stderr.txt",
             "tts": ROOT / "tmp" / "tts_wsl.log", "sup": ROOT / "tmp" / "voice-supervisor.log"}
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
LV1 = {"debug": "D", "info": "I", "warn": "W", "error": "E"}
TURN_VERDICTS = ("accept", "wake_only", "say")
BOOT_EVS = ("voice.*", "sup.*", "deps.*", "child.*", "mutex_busy", "parent_gone", "dep.change")
LINE_WIDTH = 160


# ---------- 读 ----------
def parse_time(s: str, now: float | None = None) -> float:
    """10m / 2h / 1d / 13:40 / 2026-10-11 13:40 → epoch 秒。"""
    now = time.time() if now is None else now
    s = s.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", s)
    if m:
        return now - float(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt.startswith("%H"):
            today = datetime.fromtimestamp(now)
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return dt.timestamp()
    raise argparse.ArgumentTypeError(f"看不懂的时间：{s}")


def event_files(t0: float, t1: float, log_dir: Path) -> list[Path]:
    out: list[Path] = []
    d, end = datetime.fromtimestamp(t0).date(), datetime.fromtimestamp(t1).date()
    while d <= end:
        out += sorted(log_dir.glob(f"events-*-{d:%Y-%m-%d}*.jsonl"))
        d += timedelta(days=1)
    return out


def load_events(t0: float, t1: float, log_dir: Path, needle: str | None = None) -> list[dict]:
    """读时间段内的事件；needle 是行级预筛（如 uid），先字符串匹配再解析，省时间。"""
    evs: list[dict] = []
    for f in event_files(t0, t1, log_dir):
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if needle and needle not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and t0 <= e.get("ts", 0) <= t1:
                        evs.append(e)
        except OSError:
            continue
    evs.sort(key=lambda e: e.get("ts", 0))
    return evs


# ---------- 格式 ----------
def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.") + str(int(ts * 10) % 10)


def short(v, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\r", "").replace("\n", "⏎")
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_event(e: dict, *, detail: bool = False, rel: float | None = None) -> str:
    when = f"+{round((e['ts'] - rel) * 1000):>6}ms" if rel is not None else fmt_ts(e.get("ts", 0))
    head = f"{when} {e.get('comp', '?'):<10} {LV1.get(e.get('level'), '?')} {e.get('ev', '')}"
    if e.get("uid") and rel is None:
        head += f" [{e['uid']}]"
    if e.get("msg"):
        head += " " + short(e["msg"], 80)
    data = e.get("data")
    if not data:
        return head if detail else short(head, LINE_WIDTH)
    if detail:
        return head + "\n    " + json.dumps(data, ensure_ascii=False, indent=1).replace("\n", "\n    ")
    return short(head + " | " + " ".join(f"{k}={short(v, 40)}" for k, v in data.items()), LINE_WIDTH)


class Out:
    """收集输出行，守住行数/字数上限；写文件一律 UTF-8。force=True 的行（诊断提示）不受上限约束。"""

    def __init__(self, max_lines: int, max_chars: int) -> None:
        self.max_lines, self.max_chars = max_lines, max_chars
        self.lines: list[str] = []
        self.chars = 0
        self.cut = 0

    def add(self, s: str, *, force: bool = False) -> None:
        if not force and (len(self.lines) >= self.max_lines or self.chars + len(s) + 1 > self.max_chars):
            self.cut += 1
            return
        self.lines.append(s)
        self.chars += len(s) + 1

    def text(self) -> str:
        tail = [f"…还有 {self.cut} 行没显示（加筛选、或调大 --max-lines/--max-chars）"] if self.cut else []
        return "\n".join(self.lines + tail) + "\n"

    def emit(self, path: str | None) -> None:
        text = self.text()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(text.encode("utf-8"))  # 不用 write_text：Windows 会把 \n 改成 \r\n
            print(f"written {path} lines={len(self.lines)} cut={self.cut}")  # 纯 ASCII，ssh 下不乱码
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stdout.write(text)


# ---------- 筛选 ----------
def matches(e: dict, a: argparse.Namespace) -> bool:
    if a.uid and e.get("uid") != a.uid:
        return False
    if a.comp and e.get("comp") not in a.comp:
        return False
    if LEVELS.get(e.get("level"), 20) < LEVELS[a.level]:
        return False
    if a.ev and not any(fnmatch.fnmatch(e.get("ev", ""), p) for p in a.ev):
        return False
    if a.keyword and a.keyword.lower() not in json.dumps(e, ensure_ascii=False).lower():
        return False
    return True


def list_events(a: argparse.Namespace, out: Out) -> None:
    evs = [e for e in load_events(a.since, a.until, a.log_dir, a.uid) if matches(e, a)]
    if len(evs) > a.max_lines:
        out.add(f"…前面省略 {len(evs) - a.max_lines + 1} 条（只显示最新的）")
        evs = evs[-(a.max_lines - 1):]
    for e in evs:
        out.add(json.dumps(e, ensure_ascii=False) if a.json else fmt_event(e, detail=a.detail))


# ---------- 最近几轮 ----------
def turn_line(end: dict, gw: dict | None, flag: dict | None) -> str:
    d = end.get("data") or {}
    t = d.get("timings") or {}
    g = (gw or {}).get("data") or {}
    bits = [fmt_ts(end["ts"]), f"[{end.get('uid')}]", f"{d.get('source', '')}/{d.get('verdict', '')}",
            f"「{short(d.get('text', ''), 24)}」→「{short(d.get('reply', ''), 34)}」"]
    if gw:
        tools = g.get("tools") or []
        bad = sum(1 for x in tools if x.get("ok") is False)
        bits.append(f"model={short(str(g.get('model', '')).split('/')[-1], 18)} stop={g.get('stop')} tools={len(tools)}"
                    + (f"(失败{bad})" if bad else ""))
    elif d.get("tools"):
        bits.append(f"audit={len(d['tools'])}")
    if t.get("first_sound_ms") is not None:
        bits.append(f"首音={t['first_sound_ms']}ms")
    if d.get("unspoken"):
        bits.append(f"漏念={d['unspoken']}")
    if d.get("shaped"):  # 口语整形主动省略的（不是故障）：全文在面板记录里
        bits.append(f"整形省略={d['shaped']}")
    if d.get("error"):
        bits.append("ERR=" + short(d["error"], 30))
    if flag:
        bits.append("⚑" + short(flag.get("msg", ""), 20))
    return " ".join(bits)


def recent(a: argparse.Namespace, out: Out) -> None:
    evs = load_events(a.since, a.until, a.log_dir)
    ends = [e for e in evs if e.get("ev") == "turn.end"
            and (a.all or (e.get("data") or {}).get("verdict") in TURN_VERDICTS or (e.get("data") or {}).get("source") == "nudge")]
    by_uid = lambda ev: {e.get("uid"): e for e in evs if e.get("ev") == ev and e.get("uid")}  # noqa: E731
    gws, flags = by_uid("gateway.run"), by_uid("user_flag")
    for e in ends[-a.recent:]:
        out.add(turn_line(e, gws.get(e.get("uid")), flags.get(e.get("uid"))))
    if not ends:
        out.add("（这段时间没有轮次；试 --since 1d 或 --all）")


def flags(a: argparse.Namespace, out: Out) -> None:
    n = 0
    for e in load_events(a.since, a.until, a.log_dir, "user_flag"):
        if e.get("ev") == "user_flag":
            n += 1
            d = e.get("data") or {}
            out.add(f"{fmt_ts(e['ts'])} [{e.get('uid')}] ⚑{short(e.get('msg', ''), 40)} 「{short(d.get('text', ''), 24)}」→「{short(d.get('reply', ''), 30)}」")
    if not n:
        out.add("（这段时间主人没标记过；试 --since 7d）")


# ---------- 一轮 ----------
def find_uid(uid: str, a: argparse.Namespace) -> list[dict]:
    """uid 只有时分秒：从今天往前找 a.days 天，取最近有命中的那一天。追问轮的 parent 指向被追问的那一轮，一并带出。"""
    now = time.time()
    for back in range(a.days):
        day0 = datetime.fromtimestamp(now - back * 86400).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        evs = [e for e in load_events(day0, day0 + 86399.999, a.log_dir, uid)
               if e.get("uid") == uid or (e.get("data") or {}).get("parent") == uid]
        if evs:
            return evs
    return []


def legacy_turn(uid: str, a: argparse.Namespace, out: Out) -> bool:
    """旧 utterances.jsonl 里的记录：只出 timeline 和关键字段，不重复 stages。"""
    try:
        lines = a.legacy_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        if f'"{uid}"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        out.add(f"[旧记录] {uid} {r.get('source')}/{r.get('verdict')} 「{short(r.get('text', ''), 40)}」 原因={short(r.get('reason', ''), 30)}")
        for it in r.get("timeline", []):
            out.add(f"+{round((it['t'] - r['t0']) * 1000):>6}ms {it.get('stage'):<10} {it.get('state')} {short(it.get('note', ''), 60)}")
        out.add(f"回复：{short(r.get('reply', ''), 300)}")
        if r.get("tools"):
            out.add("工具：" + "; ".join(f"{t.get('tool')} {t.get('decision')} {short(t.get('summary', ''), 50)}" for t in r["tools"]))
        if r.get("unspoken") or r.get("error"):
            out.add(f"没念出：{short(r.get('unspoken'), 120)} 错误：{r.get('error')}")
        out.add(f"耗时：{short(r.get('timings'), 200)} run_id={r.get('run_id')}")
        return True
    return False


def show_turn(uid: str, a: argparse.Namespace, out: Out) -> None:
    evs = find_uid(uid, a)
    if not evs:
        if not legacy_turn(uid, a, out):
            out.add(f"没找到 {uid}（新事件流和 utterances.jsonl 都没有）；先用 --recent 20 --all 看看编号")
        return
    t0 = evs[0]["ts"]
    end = next((e for e in reversed(evs) if e.get("ev") == "turn.end" and e.get("uid") == uid), None)
    gw = next((e for e in evs if e.get("ev") == "gateway.run"), None)
    d = (end or {}).get("data") or {}
    out.add(f"== {uid} {datetime.fromtimestamp(t0):%Y-%m-%d %H:%M:%S} {d.get('source', '')}/{d.get('verdict', '')} "
            f"「{short(d.get('text', ''), 60)}」")
    for e in evs:
        if e.get("ev") in ("turn.end", "gateway.run"):
            continue
        out.add(fmt_event(e, detail=a.detail, rel=t0))
    if end:
        out.add(f"-- 回复：{short(d.get('reply', ''), 400 if a.detail else 160)}")
        out.add(f"-- 念出 {d.get('spoken')} 句，漏念 {d.get('unspoken')} 句" + (f"，整形省略 {d['shaped']} 块" if d.get("shaped") else "")
                + f"；耗时 {short(d.get('timings'), 200)}"
                + (f"；错误 {d['error']}" if d.get("error") else ""))
    render_gateway(gw, evs, t0, a, out)
    for h in diagnose(d, (gw or {}).get("data"), evs):
        out.add("!! " + h, force=True)


# ---------- 网关段（B3）：本轮的 gateway.run 事件 + 网关日志 + 诊断提示 ----------
def gw_log_lines(t0: float, t1: float, run_id: str | None, a: argparse.Namespace, limit: int = 12) -> list[str]:
    """网关日志（%TEMP%\\openclaw\\openclaw-日期.log，JSON 行）里本轮时间窗内：含本 run_id 的行 + WARN 以上 + model-fetch 行。"""
    gdir = a.gw_log_dir or Path(os.environ.get("TEMP", tempfile.gettempdir())) / "openclaw"
    p = gdir / f"openclaw-{datetime.fromtimestamp(t0):%Y-%m-%d}.log"
    try:
        with p.open("rb") as fh:
            fh.seek(max(0, p.stat().st_size - 20_000_000))  # 最多读尾部 20MB
            raw = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in raw:
        try:
            r = json.loads(ln)
            ts = datetime.fromisoformat(r["time"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if not (t0 - 1 <= ts <= t1 + 3):
            continue
        lv = (r.get("_meta") or {}).get("logLevelName", "")
        msg = r.get("message") or " ".join(str(r[k]) for k in sorted(r) if k.isdigit())
        if (run_id and run_id in ln) or lv in ("WARN", "ERROR", "FATAL") or "[model-fetch]" in msg:
            out.append(f"+{round((ts - t0) * 1000):>6}ms gwlog {lv[:1]} {short(redact(msg), 130)}")
    return out[-limit:]


def render_gateway(gw: dict | None, evs: list[dict], t0: float, a: argparse.Namespace, out: Out) -> None:
    if not gw:
        return
    g = gw.get("data") or {}
    u = g.get("usage") or {}
    out.add(f"-- 网关 run={short(g.get('run_id', ''), 24)} {g.get('provider')}/{g.get('model')} stop={g.get('stop')} "
            f"状态={g.get('final_status')} token 入{u.get('input')}/出{u.get('output')}/缓存{u.get('cacheRead')}"
            + (f" blob={g['blob']}" if g.get("blob") else ""))
    ph = g.get("phases") or {}
    if ph:
        out.add("   阶段：" + " ".join(f"{k}=+{round((v - t0) * 1000)}ms" for k, v in sorted(ph.items(), key=lambda kv: kv[1])
                                       if isinstance(v, (int, float))))
    tools = g.get("tools") or []
    if not tools:
        out.add("   工具：本轮模型没有发起任何工具调用")
    for x in tools:
        mark = "✓" if x.get("ok") else ("✗" if x.get("ok") is False else "?")
        out.add(f"   {mark} {x.get('name')} {x.get('ms')}ms exit={x.get('exit')} {short(x.get('args', ''), 70)}"
                + (f" 错误：{short(x.get('err', ''), 80)}" if x.get("err") else ""))
    for i, txt in enumerate(g.get("texts") or []):
        out.add(f"   模型原文[{i}]：{short(txt, 400 if a.detail else 120)}")
    t1 = max(e.get("ts", t0) for e in evs)
    for ln in gw_log_lines(t0, t1, g.get("run_id"), a):
        out.add(ln)


def diagnose(end_d: dict, gw_d: dict | None, evs: list[dict]) -> list[str]:
    """看一轮的常见毛病，给一句人话。只是线索，不是结论。"""
    hints: list[str] = []
    reply = end_d.get("reply") or " ".join((gw_d or {}).get("texts") or [])
    tools = (gw_d or {}).get("tools") or []
    blocked = [e for e in evs if e.get("ev") == "tool.audit" and str((e.get("data") or {}).get("decision", "")).startswith(
        ("block", "approval-deny", "approval-timeout", "approval-cancelled"))]
    gated = blocked or any(e.get("ev") in ("tool.gate", "pending.open") for e in evs)
    if gw_d is not None:
        if not tools and has_promise(reply) and gw_d.get("stop") == "stop":
            hints.append("模型说了要做（承诺词），但这一轮一次工具调用都没发就正常结束了（stop）→ 模型行为问题，不是工具被拦；"
                         "看有没有 nudge.sent 追问、追问轮是否做了")
        bad = [x for x in tools if x.get("ok") is False]
        if bad:
            hints.append(f"工具失败 {len(bad)} 次：" + "；".join(f"{x.get('name')}: {short(x.get('err', ''), 60)}" for x in bad))
        if gw_d.get("stop") not in (None, "stop", "toolUse", "tool_calls"):
            hints.append(f"非正常结束 stopReason={gw_d.get('stop')}（length=输出被截断，error=模型侧报错）")
        if gw_d.get("aborted") or gw_d.get("timed_out"):
            hints.append(f"网关侧 aborted={gw_d.get('aborted')} timed_out={gw_d.get('timed_out')}：客户端断开或超时会取消本轮")
        if gw_d.get("prompt_error"):
            hints.append(f"提示词构建出错：{gw_d.get('prompt_error')}")
    elif end_d.get("verdict") == "accept":
        miss = any(e.get("ev") == "gateway.run_missing" for e in evs)
        hints.append("没有网关侧记录" + ("（trajectory 5 次都没查到）" if miss else "") + "：用 --runs 10 看网关最近的 run")
    if TOOLISH.search(reply):
        hints.append("回复里像混进了工具调用文本：模型想调工具但格式没被网关识别")
    if claims_blocked(reply) and not gated:
        hints.append("回复声称被拦 / 要审批 / 要放行，但本轮没有任何拦截或待确认记录（tool.gate / tool.audit）→ 疑似模型编造，"
                     "看有没有 nudge.sent（编造拦截追问）")
    if blocked:
        hints.append("分级插件拦下/审批未通过：" + "；".join(f"{(e.get('data') or {}).get('tool')} {(e.get('data') or {}).get('decision')}" for e in blocked))
    if end_d.get("unspoken"):
        hints.append(f"有 {end_d['unspoken']} 句没念出来（被打断或 TTS 失败），看 tts.* / play.* 事件")
    if end_d.get("error"):
        hints.append("本轮报错：" + short(end_d["error"], 120))
    child = next(((e.get("data") or {}).get("child") for e in evs if e.get("ev") == "nudge.sent"), None)
    if child:
        hints.append(f"本轮触发了自动追问，追问轮：--turn {child}")
    return hints


def show_runs(a: argparse.Namespace, out: Out) -> None:
    """网关最近 N 个 run（不限语音），一行一个；能对上语音 uid 的标出来。"""
    db = a.agent_db or runtrace.AGENT_DB
    uid_of = {(e.get("data") or {}).get("run_id"): e.get("uid")
              for e in load_events(time.time() - 3 * 86400, time.time() + 60, a.log_dir, "run_id")
              if e.get("ev") in ("brain.run", "gateway.run")}
    for rid in runtrace.recent_runs(a.runs, db):
        s = runtrace.run_summary(rid, db)
        if not s:
            out.add(f"{short(rid, 30)} （还没结束或读不到）")
            continue
        t = (s.get("phases") or {}).get("session_started")
        tools = s.get("tools") or []
        bad = sum(1 for x in tools if x.get("ok") is False)
        prompt = ((s.get("ctx") or {}).get("prompt")) or ""
        out.add(f"{fmt_ts(t) if t else '?'} {s.get('channel') or s.get('trigger')} uid={uid_of.get(rid, '-')} "
                f"stop={s.get('stop')} tools={len(tools)}" + (f"(失败{bad})" if bad else "")
                + f" 「{short(prompt, 30)}」→「{short(' '.join(s.get('texts') or []), 40)}」 run={short(rid, 22)}")


def show_payload(a: argparse.Namespace, out: Out) -> None:
    """发给模型的上下文摘要：先找 gateway.run 的 blob，没有就现查 trajectory（可能已滚出窗口）。"""
    key = a.payload
    if re.fullmatch(r"u\d{6}-\d+", key):
        gw = next((e for e in find_uid(key, a) if e.get("ev") == "gateway.run" and e.get("uid") == key), None)
    else:
        gw = next((e for e in load_events(time.time() - 3 * 86400, time.time() + 60, a.log_dir, key)
                   if e.get("ev") == "gateway.run"), None)
    rel = ((gw or {}).get("data") or {}).get("blob")
    text = None
    if rel:
        try:
            text = (a.log_dir / "blobs" / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = None  # blob 超过 7 天被清了：退回现查
    if text is None:
        rid = ((gw or {}).get("data") or {}).get("run_id") or key
        s = runtrace.run_summary(rid, a.agent_db or runtrace.AGENT_DB)
        if not s or not s.get("ctx"):
            out.add(f"没有 {key} 的上下文（blob 不在、trajectory 也查不到）")
            return
        text = redact(json.dumps(s["ctx"], ensure_ascii=False, indent=1))
    for ln in text.splitlines():
        out.add(ln if a.detail else short(ln, LINE_WIDTH * 2))


# ---------- 旧文本日志 ----------
def tail_text(a: argparse.Namespace, out: Out) -> None:
    p = TEXT_LOGS[a.tail]
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        out.add(f"读不了 {p}：{e}")
        return
    if a.tail == "voice":
        lines = [ln for ln in lines if "/health" not in ln]  # 每 10 秒一行的健康检查太吵
    if a.keyword:
        lines = [ln for ln in lines if a.keyword.lower() in ln.lower()]
    for ln in lines[-a.max_lines:]:
        out.add(short(ln, LINE_WIDTH * 2 if a.detail else LINE_WIDTH))


# ---------- 入口 ----------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m guanjia.logq", description="管家日志查询（默认紧凑输出）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--recent", type=int, metavar="N")
    g.add_argument("--turn", metavar="UID")
    g.add_argument("--flags", action="store_true")
    g.add_argument("--runs", type=int, metavar="N")
    g.add_argument("--payload", metavar="UID|RUN_ID")
    g.add_argument("--boot", action="store_true")
    g.add_argument("--tail", choices=sorted(TEXT_LOGS))
    ap.add_argument("--all", action="store_true", help="--recent 也列被拒/忽略的句子")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--days", type=int, default=3, help="--turn 往前找几天")
    ap.add_argument("--uid")
    ap.add_argument("--comp", type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("--level", choices=list(LEVELS), default="info")
    ap.add_argument("--ev", type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("-k", "--keyword")
    ap.add_argument("--kb64", help="base64(UTF-8) 的关键字：ssh 命令行传中文会乱码时用")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-lines", type=int, default=80)
    ap.add_argument("--max-chars", type=int, default=8000)
    ap.add_argument("--out")
    ap.add_argument("--log-dir", type=Path, default=LOG_DIR)
    ap.add_argument("--legacy-file", type=Path, default=LEGACY_RECORDS)
    ap.add_argument("--agent-db", type=Path, default=None)
    ap.add_argument("--gw-log-dir", type=Path, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    now = time.time()
    if a.kb64:
        a.keyword = base64.b64decode(a.kb64).decode("utf-8")
    filtered = any([a.uid, a.comp, a.ev, a.keyword, a.level != "info"])
    # 默认窗口：列表类（最近几轮、标记、启动、旧文本）看 1 天；按条件筛事件看 2 小时
    default_since = "2h" if filtered and not (a.boot or a.flags or a.recent or a.tail) else "1d"
    a.since = parse_time(a.since or default_since, now)
    a.until = parse_time(a.until, now) if a.until else now + 60
    out = Out(a.max_lines + 5, a.max_chars)
    if a.turn:
        show_turn(a.turn, a, out)
    elif a.flags:
        flags(a, out)
    elif a.runs:
        show_runs(a, out)
    elif a.payload:
        show_payload(a, out)
    elif a.tail:
        tail_text(a, out)
    elif a.boot:
        a.ev = a.ev or list(BOOT_EVS)
        list_events(a, out)
        warns = argparse.Namespace(**{**vars(a), "ev": None, "level": "warn"})
        out.add("-- 同期 WARN 以上 --")
        list_events(warns, out)
    elif a.recent or not filtered:
        a.recent = a.recent or 10
        recent(a, out)
    else:
        list_events(a, out)
    out.emit(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
