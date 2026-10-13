"""量一次 OpenClaw 的固定开销（每轮系统提示各部分占多少 token）。

做法：用新的测试会话键（agent:main:exp-slim-<时间>）发一句语音标记的「在吗」，非流式，取响应 usage.prompt_tokens（实测），
再读该会话的 systemPromptReport / skillsSnapshot 把字符数拆成各部分，按字符类型估算 token（中文 ≈1.45 字/token，其余 ≈4 字符/token，
JSON schema ≈3.5）。估算合计与实测的差额单列，不强行抹平。测完 sessions.delete 删掉测试会话。

只读线上配置与工作区；唯一的副作用是一次很短的模型调用和一个随即删除的测试会话。
  python scripts\\prompt_size.py                 # 跑 3 次取中位数（默认），存 tmp\\prompt-size\\<时间>.json 和 .md
  python scripts\\prompt_size.py --runs 1        # 只跑 1 次
  python scripts\\prompt_size.py --passive       # 不调模型，只读主线会话上一轮的 systemPromptReport（只有字符数拆分，没有实测）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guanjia.brain import DEFAULT_URL, load_gateway_token, tag_voice_text  # noqa: E402

try:  # P4-3 之后用包里的；P4-0 时还没有包，用 scripts/_p4_oc_fallback.py（P4-3 合并后删除）
    from guanjia.sleep.oc import AGENT_DB, gateway_call  # noqa: E402
except ImportError:
    from _p4_oc_fallback import AGENT_DB, gateway_call  # noqa: E402

OUT_DIR = ROOT / "tmp" / "prompt-size"
MAIN_KEY = "agent:main:main"


def estimate_tokens(text: str) -> int:
    """中文 ≈1.45 字/token，其余 ≈4 字符/token（与 P4 计划 §2.2 同口径）。"""
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff")
    return round(cjk / 1.45 + (len(text) - cjk) / 4)


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def load_entry(key: str, db: Path = AGENT_DB) -> dict:
    """只读打开 OpenClaw 会话库取一行 entry_json（mode=ro，不会锁住网关）；没有这行返回 {}。"""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
    try:
        row = con.execute("select entry_json from session_nodes where session_key=?", (key,)).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row and row[0] else {}


def report_for(key: str, db: Path = AGENT_DB) -> dict:
    """把 systemPromptReport 汇总成扁平字段；会话不存在或还没有报告时返回 {}。"""
    entry = load_entry(key, db)
    r = entry.get("systemPromptReport")
    if not r:
        return {}
    sp, tools = r.get("systemPrompt") or {}, (r.get("tools") or {}).get("entries") or []
    skills = r.get("skills") or {}
    turn = r.get("currentTurn") or {}
    return {
        "system_chars": sp.get("chars"),
        "project_chars": sp.get("projectContextChars"),
        "non_project_chars": sp.get("nonProjectContextChars"),
        "files": {f["name"]: f.get("injectedChars") for f in r.get("injectedWorkspaceFiles") or []},
        "file_paths": {f["name"]: f.get("path") for f in r.get("injectedWorkspaceFiles") or []},
        "skills": [s["name"] for s in skills.get("entries") or []],
        "skills_chars": skills.get("promptChars"),
        "skill_blocks": {s["name"]: s.get("blockChars", 0) for s in skills.get("entries") or []},
        "skills_prompt": (entry.get("skillsSnapshot") or {}).get("prompt") or "",
        "tools": [t["name"] for t in tools],
        "tool_summary_chars": sum(t.get("summaryChars", 0) for t in tools),
        "tool_schema_chars": sum(t.get("schemaChars", 0) for t in tools),
        "tool_chars": {t["name"]: [t.get("summaryChars", 0), t.get("schemaChars", 0)] for t in tools},
        "turn_chars": (turn.get("promptChars") or 0) + (turn.get("runtimeContextChars") or 0),
        "estimated_prompt_tokens": (entry.get("contextBudgetStatus") or {}).get("estimatedPromptTokens"),
    }


def breakdown(rep: dict, file_texts: dict[str, str] | None = None) -> dict:
    """各部分估算 token。file_texts：工作区文件名 -> 文本（读不到时按 字符/4 估）。返回 rows / skills / tools / total。"""
    file_texts = file_texts or {}
    rows: list[dict] = []
    for name, chars in (rep.get("files") or {}).items():
        text = file_texts.get(name)
        rows.append({"part": name, "group": "工作区文件", "chars": chars or 0,
                     "tokens": estimate_tokens(text) if text is not None else round((chars or 0) / 4)})
    skills_chars = rep.get("skills_chars") or 0
    skills_tokens = estimate_tokens(rep["skills_prompt"]) if rep.get("skills_prompt") else round(skills_chars / 4)
    rows.append({"part": "技能列表", "group": "技能", "chars": skills_chars, "tokens": skills_tokens})
    summary, schema = rep.get("tool_summary_chars") or 0, rep.get("tool_schema_chars") or 0
    rows.append({"part": "工具摘要", "group": "工具", "chars": summary, "tokens": round(summary / 4)})
    rows.append({"part": "工具 schema", "group": "工具", "chars": schema, "tokens": round(schema / 3.5)})
    builtin = max(0, (rep.get("non_project_chars") or 0) - skills_chars - summary)
    rows.append({"part": "OpenClaw 自带段落", "group": "自带（不可配）", "chars": builtin, "tokens": round(builtin / 4)})
    rows.append({"part": "本轮用户话+运行时", "group": "本轮", "chars": rep.get("turn_chars") or 0,
                 "tokens": round((rep.get("turn_chars") or 0) / 2)})
    blocks = rep.get("skill_blocks") or {}
    block_sum = sum(blocks.values()) or 1
    skills = {n: round(skills_tokens * c / block_sum) for n, c in blocks.items()}
    tools = {n: round(s / 4 + c / 3.5) for n, (s, c) in (rep.get("tool_chars") or {}).items()}
    return {"rows": rows, "skills": skills, "tools": tools, "total": sum(r["tokens"] for r in rows)}


def heartbeat_info(rpc=gateway_call) -> dict:
    """心跳（后台）：读 cron 里那条 heartbeat 任务和最近几次的结果。只读；网关连不上时把原因放进 error。"""
    try:
        jobs = rpc("cron.list", {}).get("jobs") or []
        hb = next((j for j in jobs if (j.get("payload") or {}).get("kind") == "heartbeat"), None)
        if not hb:
            return {"present": False}
        runs = rpc("cron.runs", {"id": hb["id"], "limit": 5}).get("entries") or []
        every = (hb.get("schedule") or {}).get("everyMs")
        return {"present": True, "every_min": round(every / 60000) if every else None, "recent": len(runs),
                "recent_all_skipped": bool(runs) and all(e.get("status") == "skipped" for e in runs),
                "recent_errors": sorted({e.get("error") for e in runs if e.get("error")}),
                "recent_ms": [e.get("durationMs") for e in runs]}
    except Exception as e:  # 心跳信息只是附注，取不到不影响测量
        return {"error": str(e)[:200]}


def read_files(rep: dict) -> dict[str, str]:
    """读报告里记录的工作区文件原文（只读），用来按中英文比例估 token。读不到的跳过。"""
    out = {}
    for name, p in (rep.get("file_paths") or {}).items():
        try:
            out[name] = Path(p).read_text(encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass
    return out


def probe_once(url: str, token: str) -> dict:
    """新测试会话发一句，返回 usage + 该会话的报告；用完删除测试会话。"""
    key = f"agent:main:exp-slim-{time.strftime('%m%d%H%M%S')}"
    r = httpx.post(url, headers={"Authorization": f"Bearer {token}", "x-openclaw-session-key": key},
                   json={"model": "openclaw", "messages": [{"role": "user", "content": tag_voice_text("在吗", speaker="主人")}]},
                   timeout=120, trust_env=False)
    r.raise_for_status()
    usage = r.json().get("usage") or {}
    rep: dict = {}
    for _ in range(10):  # 报告在响应之后才落库，最多等 3 秒
        rep = report_for(key)
        if rep:
            break
        time.sleep(0.3)
    out = {"key": key, "usage": usage, "report": rep}
    try:
        gateway_call("sessions.delete", {"key": key})
    except Exception as e:  # 删不掉不影响测量结果，留着手工删
        out["delete_error"] = str(e)[:200]
    return out


def render_md(result: dict) -> str:
    bd, total = result["breakdown"], result.get("median_prompt_tokens")
    lines = [f"# 提示词体积基线 {result['t']}", "",
             f"- 模式：{result['mode']}；实测 prompt_tokens：{result.get('prompt_tokens_runs')}，中位数 {total}",
             f"- 技能 {len(result['report']['skills'])} 个，工具 {len(result['report']['tools'])} 个；"
             f"系统提示 {result['report']['system_chars']} 字符（项目上下文 {result['report']['project_chars']}，非项目 {result['report']['non_project_chars']}）",
             "- token 为估算（中文 1.45 字/token，英文 4 字符/token，schema 3.5），合计与实测的差额单列", "",
             "| 部分 | 类别 | 字符 | 估算 token | 占比 |", "|---|---|---:|---:|---:|"]
    base = total or bd["total"] or 1
    for r in bd["rows"]:
        lines.append(f"| {r['part']} | {r['group']} | {r['chars']} | {r['tokens']} | {r['tokens'] / base:.0%} |")
    lines.append(f"| **估算合计** | | | **{bd['total']}** | {bd['total'] / base:.0%} |")
    if total:
        lines.append(f"| 实测 prompt_tokens | | | **{total:g}** | 差额 {total - bd['total']:+g}（模板/工具定义包装等） |")
    lines += ["", "## 技能（估算 token，大到小）", ""]
    lines += [f"- {n}: {t}" for n, t in sorted(bd["skills"].items(), key=lambda kv: -kv[1])]
    lines += ["", "## 工具（摘要+schema，估算 token，大到小）", ""]
    lines += [f"- {n}: {t}" for n, t in sorted(bd["tools"].items(), key=lambda kv: -kv[1])]
    hb = result.get("heartbeat") or {}
    lines += ["", "## 心跳（后台，不在每轮 prompt 里）", "", f"- {json.dumps(hb, ensure_ascii=False)}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="量 OpenClaw 每轮固定开销（只读 + 一次很短的测试调用）")
    ap.add_argument("--runs", type=int, default=3, help="测几次取中位数（默认 3）")
    ap.add_argument("--passive", action="store_true", help="不调模型，只读主线会话上一轮的报告")
    ap.add_argument("--url", default=DEFAULT_URL)
    a = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    result: dict = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "passive" if a.passive else f"probe x{a.runs}"}
    if a.passive:
        rep = report_for(MAIN_KEY)
        if not rep:
            print("主线会话还没有 systemPromptReport")
            return 1
        result.update(report=rep, prompt_tokens_runs=[], median_prompt_tokens=rep.get("estimated_prompt_tokens"))
    else:
        token, runs = load_gateway_token(), []
        for _ in range(a.runs):
            runs.append(probe_once(a.url, token))
            time.sleep(1.1)  # 会话键按秒取名，避免同秒重名
        toks = [r["usage"].get("prompt_tokens") for r in runs if r["usage"].get("prompt_tokens")]
        if not toks or not runs[-1]["report"]:
            print(json.dumps({"error": "响应没有 usage.prompt_tokens 或读不到报告", "runs": runs}, ensure_ascii=False)[:800])
            return 1
        result.update(report=runs[-1]["report"], prompt_tokens_runs=toks, median_prompt_tokens=median(toks),
                      usage=runs[-1]["usage"], delete_errors=[r["delete_error"] for r in runs if "delete_error" in r])
    result["breakdown"] = breakdown(result["report"], read_files(result["report"]))
    result["heartbeat"] = heartbeat_info()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render_md(result)
    (OUT_DIR / f"{stamp}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
