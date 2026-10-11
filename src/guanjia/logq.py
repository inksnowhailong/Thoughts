"""日志查询：python -m guanjia.logq [选项]。默认紧凑（一事件一行、截断长字段、总量有上限），--detail 才展开。

数据源（全部只读）：
    tmp\\logs\\events-*.jsonl        新事件流（各进程一份，合并后按 ts 排序）
    tmp\\utterances.jsonl            旧记录（--turn 找不到新事件时兜底）
    网关 trajectory / 网关日志       见 B3（--turn 的网关段、--runs、--payload）
远程用法：scripts\\logq.cmd 会加 --out tmp\\logq-out.txt，再 scp 回来读，绕开 ssh 的 GBK 乱码。
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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
        out.add(f"-- 念出 {d.get('spoken')} 句，漏念 {d.get('unspoken')} 句；耗时 {short(d.get('timings'), 200)}"
                + (f"；错误 {d['error']}" if d.get("error") else ""))
    render_gateway(gw, evs, t0, a, out)  # B3 实现；B2 阶段先是空函数
    for h in diagnose(d, (gw or {}).get("data"), evs):  # B3 补全；B2 阶段只做本地规则
        out.add("!! " + h, force=True)


def render_gateway(gw, evs, t0, a, out) -> None:  # B3 替换
    pass


def diagnose(end_d: dict, gw_d: dict | None, evs: list[dict]) -> list[str]:  # B3 扩充
    hints = []
    if end_d.get("unspoken"):
        hints.append(f"有 {end_d['unspoken']} 句没念出来（被打断或 TTS 失败），看上面 tts.* / play.* 事件")
    if end_d.get("error"):
        hints.append("本轮报错：" + short(end_d["error"], 120))
    child = next(((e.get("data") or {}).get("child") for e in evs if e.get("ev") == "nudge.sent"), None)
    if child:
        hints.append(f"本轮触发了自动追问，追问轮：--turn {child}")
    return hints


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
        show_runs(a, out)  # B3
    elif a.payload:
        show_payload(a, out)  # B3
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


def show_runs(a, out) -> None:  # B3 替换
    out.add("（--runs 在 B3 实现）")


def show_payload(a, out) -> None:  # B3 替换
    out.add("（--payload 在 B3 实现）")


if __name__ == "__main__":
    raise SystemExit(main())
