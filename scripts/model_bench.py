"""硅基流动模型实测：直连 OpenAI 兼容接口，对比「现在的主/备模型」与候选模型在管家语音场景下的表现。

不走 OpenClaw 网关、不碰主线会话；工具只判定不执行（给假结果再跑一轮，看最终回答是否守规则）。
密钥只在进程内存里用（读 ~/.openclaw/openclaw.json），任何输出、日志都不含密钥。

用法（在家里电脑上，用项目 .venv）：
    python scripts/model_bench.py run  --keys v4flash,q35-35b --mode lean --tag a --budget 3.0
    python scripts/model_bench.py aux            # 顺带测硅基的 嵌入/ASR/TTS 延迟
    python scripts/model_bench.py summarize      # 汇总 tmp/model_bench/*.jsonl -> summary.md / summary.json

结果写在 tmp/model_bench/；可断点续跑（已跑过的 模型/模式/用例/序号 会跳过）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics as st
import sys
import time
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(os.environ.get("GUANJIA_BENCH_OUT") or ROOT / "tmp" / "model_bench")
OC = Path.home() / ".openclaw"
WS = OC / "workspace"
BASE = "https://api.siliconflow.cn/v1"
TAG = "[语音 2026-10-11 14:30 周日]"  # 与线上 render_voice_tag 的格式一致
REMINDER = ""  # --reminder：附在语音轮用户消息末尾的一行规则提醒（试验"靠近末尾的提醒能否提高守规则率"）
MAX_ROUNDS = 6  # 一轮对话里模型最多连续调几轮（含最后的口头答复）
MONTH_TURNS = 3000  # 主人说的"每天 100 轮"×30 天
# 线上真实一轮（agent 库 trajectory 里 19 条语音轮的均值）：新输入≈6.7k、缓存命中≈21.7k token（含约 19% 冷启动）；前缀稳定时≈1.2k + 26k
REAL_AVG = (6700, 21700)
REAL_WARM = (1200, 26000)

# ---------------------------------------------------------------- 候选模型
# price = (未缓存输入, 缓存命中输入, 输出) 元/百万 token，来自官网定价页（2026-10-11 实查）；
# 缓存价页面未列的，按"命中也收全价"保守估算。
MODELS: dict[str, dict] = {
    # —— 基准 ——
    "v4flash": dict(id="deepseek-ai/DeepSeek-V4-Flash", extra={"enable_thinking": False}, price=(3.0, 0.30, 9.0),
                    tier="基准·主", note="白天档；凌晨2-8点半价"),
    "q35-35b": dict(id="Qwen/Qwen3.5-35B-A3B", extra={"enable_thinking": False}, price=(0.4, 0.4, 3.2),
                    tier="基准·备", note="≤128k档价"),
    # —— 用户点名：GLM-5.3-Flash 的两种"关推理" ——
    "glm53f-low": dict(id="zai-org/GLM-5.3-Flash", extra={"reasoning_effort": "low"}, price=(0.8, 0.23, 2.8),
                       tier="同价位", note="reasoning_effort=low"),
    "glm53f-off": dict(id="zai-org/GLM-5.3-Flash", extra={"thinking": {"type": "disabled"}}, price=(0.8, 0.23, 2.8),
                       tier="同价位", note="thinking.type=disabled"),
    # —— 同价位/略贵的其它候选 ——
    "q38-27b": dict(id="Qwen/Qwen3.8-27B", extra={"enable_thinking": False}, price=(3.0, 3.0, 12.0),
                    tier="同价位", note=""),
    "q36-35b": dict(id="Qwen/Qwen3.6-35B-A3B", extra={"enable_thinking": False}, price=(1.8, 1.8, 10.8),
                    tier="同价位", note=""),
    "step35f": dict(id="stepfun-ai/Step-3.5-Flash", extra={"enable_thinking": False}, price=(0.7, 0.7, 2.1),
                    tier="同价位", note="推理模型，关不干净"),
    # —— 免费 ——
    "xing29b": dict(id="XingChenAGI/Xing4.0-29B", extra={"enable_thinking": False}, price=(0.0, 0.0, 0.0),
                    tier="免费", note="中国电信星辰，256K"),
    "q3-8b": dict(id="Qwen/Qwen3-8B", extra={"enable_thinking": False}, price=(0.0, 0.0, 0.0), tier="免费", note=""),
    "q35-4b": dict(id="Qwen/Qwen3.5-4B", extra={"enable_thinking": False}, price=(0.0, 0.0, 0.0), tier="免费",
                   note="价格取自模型详情数据，需控制台复核"),
    "glm4-9b": dict(id="THUDM/GLM-4-9B-0414", extra={}, price=(0.0, 0.0, 0.0), tier="免费", note="上下文仅32K"),
    # —— 愿意多花钱 ——
    "v4pro": dict(id="deepseek-ai/DeepSeek-V4-Pro", extra={"enable_thinking": False}, price=(12.0, 1.0, 24.0),
                  tier="多花钱", note="只跑 1 轮", n=1),
    "glm53": dict(id="zai-org/GLM-5.3", extra={"reasoning_effort": "low"}, price=(8.0, 2.0, 28.0),
                  tier="多花钱", note="只跑 1 轮", n=1),
}

# ---------------------------------------------------------------- 用例
# expect: none=不该调工具；weather；launch=launch_app music；act=该动手（任一工具）；act_or_ask=动手或一句话澄清；
#         delete=该先调 exec（会被拦），拿到"需要确认"后只问一句；note=exec/write 建文件
CASES: list[dict] = [
    dict(id="time", text="现在几点了", voice=True, expect="none"),
    dict(id="weather", text="明天天气怎么样", voice=True, expect="weather"),
    dict(id="music", text="打开网易云", voice=True, expect="launch"),
    dict(id="spk_soft", text="帮我把音箱的控制软件打开", voice=True, expect="act"),
    dict(id="halo1", text="人家帮我把这个 H A L 的这个音响的驱动和我打", voice=True, expect="act_or_ask"),
    dict(id="halo2", text="不对我说的是花在的 H A L O 的这个音响就是当前咱们电脑装的这个音响", voice=True,
         expect="act_or_ask"),
    dict(id="note", text="帮我在桌面建个记事本写上买牛奶", voice=True, expect="note"),
    dict(id="delete", text="把 D 盘的旧日志删了", voice=True, expect="delete"),
    dict(id="chat1", text="你今天心情怎么样", voice=True, expect="none"),
    dict(id="joke", text="给我讲个笑话吧", voice=True, expect="none"),
    dict(id="typo_weather", text="帮我查一下北京今天的天汽怎么样", voice=True, expect="weather"),
    dict(id="volume", text="把电脑音量调到百分之五十", voice=True, expect="act"),
    dict(id="wx_long", voice=False, expect="none",
         text="我最近在考虑换一台笔记本电脑，预算八千左右，主要用来写代码和偶尔打游戏，你觉得该怎么选配置，内存和显卡要多少合适？"),
    dict(id="dl_search", text="帮我查一下漫步者 Halo 音响的官方软件在哪下载", voice=True, expect="act"),
    dict(id="noise", text="嗯嗯那个啥呃", voice=True, expect="none"),
]
LONG_CASES = ["time", "weather", "music", "halo2"]  # 长上下文模式只跑这几条（控成本）

# ---------------------------------------------------------------- 提示词与工具定义


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def _tool_descs() -> dict[str, str]:
    """线上「有效工具清单」里的真实工具描述（tmp/tools_eff_voice.json）。"""
    p = ROOT / "tmp" / "tools_eff_voice.json"
    out: dict[str, str] = {}
    try:
        j = json.loads(_read(p))
        for g in j["groups"]:
            for t in g["tools"]:
                out[t["id"]] = " ".join((t.get("rawDescription") or t.get("description") or "").split())
    except Exception:
        pass
    return out


def build_system(mode: str) -> str:
    """lean=AGENTS 管家规则段+SOUL；long=再补全文 AGENTS、三个技能、全部工具描述，拉到接近线上约 4.7 万字符。"""
    agents = _read(WS / "AGENTS.md")
    m = re.search(r"<!-- guanjia-s2:begin -->(.*?)<!-- guanjia-s2:end -->", agents, re.S)
    rules = m.group(1).strip() if m else agents
    soul = _read(WS / "SOUL.md")
    runtime = ("## Runtime\n电脑：家里的 Windows 11 台式机，PowerShell；工作区 C:\\Users\\inksn\\.openclaw\\workspace。\n"
               "当前时间以用户消息开头的标记为准。你通过音箱和主人说话，回复会被合成语音念出来。")
    base = f"{soul}\n\n---\n\n{rules}\n\n---\n\n{runtime}"
    if mode == "lean":
        return base
    parts = [base, "\n\n# Project Context\n\n## AGENTS.md\n\n" + agents, "\n\n## SOUL.md\n\n" + soul]
    for sk in ("guanjia-launch", "guanjia-lookup", "guanjia-notes"):
        p = WS / "skills" / sk / "SKILL.md"
        if p.exists():
            parts.append(f"\n\n## Skill: {sk}\n\n" + _read(p))
    descs = _tool_descs()
    parts.append("\n\n## Tool catalog\n\n" + "\n".join(f"- {k}: {v}" for k, v in descs.items()))
    return "".join(parts)


def _fn(name: str, desc: str, props: dict, req: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
                                              "parameters": {"type": "object", "properties": props, "required": req}}}


def build_tools() -> list[dict]:
    """模拟线上 12 个直接可见的工具；launch_app/get_weather/get_location/speak 是"延期工具"，经 tool_search/tool_call 调。"""
    d = _tool_descs()
    s = {"type": "string"}
    return [
        _fn("exec", d.get("exec") or "Run shell now.", {"command": s, "workdir": s, "timeout": {"type": "number"}}, ["command"]),
        _fn("process", d.get("process") or "Inspect/control exec sessions.", {"action": s, "sessionId": s}, ["action"]),
        _fn("read", d.get("read") or "Read text file.", {"path": s}, ["path"]),
        _fn("write", d.get("write") or "Write/overwrite file; creates parent directories.", {"path": s, "content": s},
            ["path", "content"]),
        _fn("edit", d.get("edit") or "Exact single-file replacements.", {"path": s, "edits": {"type": "array", "items": {"type": "object"}}},
            ["path", "edits"]),
        _fn("ls", d.get("ls") or "List directory entries.", {"path": s}, []),
        _fn("web_fetch", d.get("web_fetch") or "Fetch URL; extract readable markdown/text.", {"url": s}, ["url"]),
        _fn("web_search", d.get("web_search") or "Search current web.", {"query": s}, ["query"]),
        _fn("browser", d.get("browser") or "Control the browser.", {"action": s, "url": s}, ["action"]),
        _fn("tool_search", "Search the deferred tool catalog. Returns candidate tool ids you can run with tool_call.",
            {"queries": {"type": "array", "items": {"type": "object", "properties": {"query": s, "limit": {"type": "number"}}}}},
            ["queries"]),
        _fn("tool_describe", "Show the input schema of a deferred tool by id.", {"id": s}, ["id"]),
        _fn("tool_call", "Call a deferred tool by id with args (find ids via tool_search; AGENTS.md lists the common ones).",
            {"id": s, "args": {"type": "object"}}, ["id"]),
    ]


# ---------------------------------------------------------------- 假工具结果
CATALOG = [
    {"id": "launch_app", "description": "Launch a game or program registered in apps.json on the home PC."},
    {"id": "get_weather", "description": "Current weather plus today's and tomorrow's forecast."},
    {"id": "get_location", "description": "Get the home PC's current latitude/longitude."},
    {"id": "speak", "description": "Speak a sentence aloud on the home PC speakers."},
]


def fake_tool(name: str, args: dict | None, case_id: str = "") -> str:
    """工具只判定不执行：按名字和参数造一个像样的假结果。"""
    a = args or {}
    if name == "tool_search":
        return json.dumps({"results": [{"candidates": CATALOG}]}, ensure_ascii=False)
    if name == "tool_describe":
        return json.dumps({"id": a.get("id"), "input": {"type": "object"}}, ensure_ascii=False)
    if name == "tool_call":
        tid = str(a.get("id") or "").split(":")[-1]
        inner = a.get("args") if isinstance(a.get("args"), dict) else {}
        if tid == "launch_app":
            if inner.get("name") == "music":
                return "已启动 网易云音乐。"
            return f"没有启动：apps.json 里没有「{inner.get('name')}」。已登记的有：lol、naraka、bf6、steam、wegame、music。"
        if tid == "get_weather":
            return "家这边：现在22度（体感21度），多云；今天18到25度，多云，降水概率10%；明天19到26度，晴，降水概率0%。不用带伞。"
        if tid == "get_location":
            return "纬度 39.97，经度 116.55（电脑自己定位到的）。"
        if tid == "speak":
            return "已在电脑上念出。"
        if tid in ("exec", "web_fetch", "read", "write", "ls"):
            return fake_tool(tid, inner, case_id)
        return "没有这个工具：" + tid
    if name == "exec":
        cmd = str(a.get("command") or "")
        if re.search(r"Remove-Item|\bdel\b|\brm\b|rmdir|erase", cmd, re.I):  # 与线上 guard.ts 的拦截话术一致
            return (f"需要确认（删除文件）：exec: {cmd[:80]}。先用一句口语问用户要不要这么做，不要让用户去微信或网页里点。"
                    "用户说「允许 / 可以 / 行」就做这一次；这类每次都要问，不要提「以后都允许」。用户同意后，用完全相同的参数再调用一次；不要换写法绕过。")
        if case_id == "delete" and re.search(r"Get-ChildItem|\bdir\b|\bls\b|Where-Object|Select-Object|Get-Item|Test-Path|Get-PSDrive", cmd, re.I):
            return "D:\\logs\\app-2026-05.log  45MB  2026-05-30\nD:\\logs\\app-2026-06.log  38MB  2026-06-29\nD:\\logs\\app-2026-09.log  12MB  2026-09-30"
        if re.search(r"volume|Audio|nircmd|SendKeys|IAudioEndpointVolume", cmd, re.I):
            return "音量已设为 50%。"
        if re.search(r"Get-Date|date /t|time /t", cmd, re.I):
            return "2026-10-11 14:30"
        if re.search(r"PnpDevice|driver|Win32_PnPSigned|pnputil|Get-WmiObject|Get-CimInstance", cmd, re.I):
            return "Status Name\nOK     EDIFIER Halo Soundbar (驱动 2.1.4，已安装)"
        if re.search(r"Get-Package|Get-StartApps|Uninstall|where\.exe|Get-ChildItem|dir |Get-Command|Get-AppxPackage", cmd, re.I):
            return "EDIFIER Connect    C:\\Program Files\\EDIFIER Connect\\EdifierConnect.exe"
        if re.search(r"Start-Process|\bstart\b|Invoke-Item|explorer", cmd, re.I):
            return "已启动。"
        if re.search(r"New-Item|Set-Content|Out-File|echo|notepad", cmd, re.I):
            return "已完成，文件已创建。"
        return "（命令已执行，无输出，退出码 0）"
    if name == "write":
        return f"已写入 {a.get('path')}"
    if name == "web_fetch":
        return "（网页正文摘要）漫步者 EDIFIER Connect 官方下载页：Windows 版 v3.2.1，支持 Halo 系列音响，下载地址在页面“软件下载”栏。"
    if name == "web_search":
        return "1. 漫步者官网 软件下载 https://www.edifier.com.cn/download  2. EDIFIER Connect Windows 版 v3.2.1"
    if name == "read":
        return "（文件内容略）"
    if name == "ls":
        return "Desktop\nDocuments\nDownloads"
    return "ok"


# ---------------------------------------------------------------- 判定


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?；;\n]+", text)
    return [p.strip() for p in parts if re.search(r"[\u4e00-\u9fffA-Za-z0-9]", p)]


MD_RE = re.compile(r"(\*\*|^\s*#{1,6}\s|^\s*[-*•]\s|^\s*\d+[.、)]\s|`|\[[^\]]*\]\([^)]*\)|^\s*\|.*\|)", re.M)
PROMISE_RE = re.compile(r"(我先|让我|我来查|我去|我这就|我看看|稍等|马上|帮你查|我查一下|我试试|这就去)")
DONE_RE = re.compile(r"(已经|已为你|已帮你|搞定|好了|已打开|已启动|已删除|删除了|删好了|已创建|建好了|已调)")
LEAK_RE = re.compile(r"(Let me|I will|I'll|The user|<think>|</think>)")


def check_rules(spoken: str, voice: bool) -> dict:
    sents = _sentences(spoken)
    lens = [len(re.sub(r"[\s，,、：:]", "", s)) for s in sents]
    md = bool(MD_RE.search(spoken))
    leak = bool(LEAK_RE.search(spoken))
    if voice:
        ok = len(sents) <= 2 and (max(lens) if lens else 0) <= 30 and not md and not leak
    else:  # 微信等无语音标记：默认不超两句（"要我展开讲吗"这类收尾不算）、无 Markdown
        core = [s for s in sents if "展开" not in s]
        ok = len(core) <= 2 and not md and not leak
    return {"sentences": len(sents), "max_len": max(lens) if lens else 0, "markdown": md, "leak": leak, "rules_ok": ok}


def _actions(calls: list[dict]) -> list[tuple[str, str]]:
    """把调用展开成 (名字, 内层id)：tool_call 看内层 id。"""
    out = []
    for c in calls:
        a = c.get("args") if isinstance(c.get("args"), dict) else {}
        inner = str(a.get("id") or "").split(":")[-1] if c["name"] == "tool_call" else ""
        out.append((c["name"], inner))
    return out


def judge(case: dict, calls: list[dict], spoken: str, final: str) -> dict:
    acts = _actions(calls)
    expect = case["expect"]
    names = [n for n, _ in acts]
    inners = [i for n, i in acts if n == "tool_call"]
    ask = (not calls) and bool(re.search(r"[？?]|吗$", final.strip())) and not PROMISE_RE.search(final)
    tool_ok = False
    alt = False
    if expect == "none":
        tool_ok = not calls
    elif expect == "weather":
        tool_ok = "get_weather" in inners
        if not tool_ok and any(n == "web_fetch" and re.search(r"weather|meteo", json.dumps(c.get("args") or {}), re.I)
                               for n, c in zip(names, calls)):
            tool_ok, alt = True, True
    elif expect == "launch":
        for (n, i), c in zip(acts, calls):
            inner = (c.get("args") or {}).get("args") if isinstance(c.get("args"), dict) else None
            if n == "tool_call" and i == "launch_app" and isinstance(inner, dict) and inner.get("name") == "music":
                tool_ok = True
    elif expect == "act":
        tool_ok = bool(calls)
    elif expect == "act_or_ask":
        tool_ok = bool(calls) or ask
    elif expect == "delete":
        tool_ok = "exec" in names or "exec" in inners
    elif expect == "note":
        tool_ok = any(n in ("exec", "write", "edit") for n in names) or any(i in ("exec", "write") for i in inners)
    needs_tool = expect not in ("none",)
    talk_only = needs_tool and not calls and not (expect == "act_or_ask" and ask)
    fabricated = needs_tool and not calls and bool(DONE_RE.search(final))
    promise = bool(PROMISE_RE.search(spoken))
    # 最终回答是否合理（只看关键信息，不评文采）
    fin = final
    if expect == "weather":
        final_ok = "度" in fin
    elif expect == "launch":
        final_ok = bool(re.search(r"(启动|打开|好了|已)", fin))
    elif expect == "delete":
        final_ok = bool(re.search(r"[？?]|吗|允许|确认", fin)) and not DONE_RE.search(fin)
    elif case["id"] == "time":
        final_ok = any(s in fin for s in ("2点30", "两点半", "14:30", "2:30", "14点30", "2点半", "两点三十", "2点三十"))
    elif case["id"] == "noise":
        final_ok = bool(re.search(r"(没听清|再说|？|\?)", fin))
    else:
        final_ok = bool(fin.strip())
    return {"tool_ok": tool_ok, "alt_ok": alt, "talk_only": talk_only, "fabricated": fabricated, "promise": promise,
            "asked": ask, "final_ok": final_ok}


# ---------------------------------------------------------------- 请求


def load_key() -> str:
    cfg = json.loads((OC / "openclaw.json").read_text(encoding="utf-8"))
    return cfg["models"]["providers"]["siliconflow"]["apiKey"]


def call_cost(price: tuple, usage: dict | None, guard: bool = False) -> float:
    """元。guard=True 不算缓存折扣，用于预算保护（偏保守）。"""
    if not usage:
        return 0.0
    pin, pc, pout = price
    pt = usage.get("prompt_tokens") or 0
    hit = 0 if guard else (usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return ((pt - hit) * pin + hit * pc + (usage.get("completion_tokens") or 0) * pout) / 1e6


def stream_chat(client: httpx.Client, key: str, mc: dict, messages: list[dict], tools: list[dict], max_tokens: int = 600) -> dict:
    """一次流式请求。返回首 token/首个正文字时间（相对本次请求发出）、正文、工具调用、用量。"""
    body = {"model": mc["id"], "messages": messages, "tools": tools, "stream": True,
            "stream_options": {"include_usage": True}, "max_tokens": max_tokens, **mc["extra"]}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    retries = 0
    last_err = ""
    for attempt in range(6):
        t0 = time.perf_counter()
        first_any = first_content = None
        content, reasoning, usage, finish = "", 0, None, None
        tcs: dict[int, dict] = {}
        try:
            with client.stream("POST", f"{BASE}/chat/completions", headers=headers, json=body) as r:
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code} {r.read()[:200].decode('utf-8', 'replace')}"
                    if r.status_code in (429, 500, 502, 503, 504):
                        retries += 1
                        time.sleep(min(30, 4 * 2 ** attempt))
                        continue
                    return {"error": last_err, "retries": retries}
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    p = line[5:].strip()
                    if p == "[DONE]":
                        break
                    try:
                        j = json.loads(p)
                    except json.JSONDecodeError:
                        continue
                    if j.get("usage"):
                        usage = j["usage"]
                    ch = (j.get("choices") or [{}])[0]
                    d = ch.get("delta") or {}
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    now = time.perf_counter() - t0
                    if d.get("reasoning_content"):
                        reasoning += len(d["reasoning_content"])
                        first_any = first_any if first_any is not None else now
                    if d.get("content"):
                        first_any = first_any if first_any is not None else now
                        if first_content is None and d["content"].strip():
                            first_content = now
                        content += d["content"]
                    for tc in d.get("tool_calls") or []:
                        first_any = first_any if first_any is not None else now
                        slot = tcs.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
            return {"t_start": t0, "elapsed": time.perf_counter() - t0, "ttft_any": first_any, "ttft_content": first_content,
                    "content": content, "reasoning_chars": reasoning, "usage": usage, "finish": finish, "retries": retries,
                    "tool_calls": [tcs[i] for i in sorted(tcs)]}
        except (httpx.HTTPError, OSError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:150]}"
            retries += 1
            time.sleep(3)
    return {"error": last_err or "unknown", "retries": retries}


def run_turn(client: httpx.Client, key: str, mc: dict, system: str, tools: list[dict], case: dict) -> dict:
    user = f"{TAG} {case['text']}{REMINDER}" if case["voice"] else case["text"]
    msgs: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    t_turn0 = time.perf_counter()
    rounds, calls, spoken_parts = [], [], []
    first_speak = first_tool = None
    guard_cost = cost = 0.0
    err = None
    for rd in range(MAX_ROUNDS):
        r = stream_chat(client, key, mc, msgs, tools)
        if "error" in r:
            err = r["error"]
            rounds.append({"error": err, "retries": r.get("retries", 0)})
            break
        off = r["t_start"] - t_turn0
        if r["ttft_content"] is not None and first_speak is None:
            first_speak = off + r["ttft_content"]
        cost += call_cost(mc["price"], r["usage"])
        guard_cost += call_cost(mc["price"], r["usage"], guard=True)
        parsed = []
        for i, tc in enumerate(r["tool_calls"]):
            try:
                args = json.loads(tc["args"] or "{}")
            except json.JSONDecodeError:
                args = None
            parsed.append({"id": tc["id"] or f"call_{rd}_{i}", "name": tc["name"], "args": args, "raw": tc["args"][:300]})
        if parsed and first_tool is None and r["ttft_any"] is not None:
            first_tool = off + r["ttft_any"]
        rounds.append({"ttft_any": r["ttft_any"], "ttft_content": r["ttft_content"], "elapsed": round(r["elapsed"], 3),
                       "reasoning_chars": r["reasoning_chars"], "usage": r["usage"], "text": r["content"], "finish": r["finish"],
                       "retries": r["retries"], "tools": [{"name": p["name"], "args": p["args"]} for p in parsed]})
        if r["content"].strip():
            spoken_parts.append(r["content"].strip())
        calls.extend(parsed)
        if not parsed:
            break
        msgs.append({"role": "assistant", "content": r["content"], "tool_calls": [
            {"id": p["id"], "type": "function", "function": {"name": p["name"], "arguments": p["raw"] if p["args"] is None else json.dumps(p["args"], ensure_ascii=False)}}
            for p in parsed]})
        for p in parsed:
            res = "参数不是合法 JSON，请重新调用。" if p["args"] is None else fake_tool(p["name"], p["args"], case["id"])
            msgs.append({"role": "tool", "tool_call_id": p["id"], "content": res})
    spoken = "\n".join(spoken_parts)
    final = rounds[-1].get("text", "") if rounds and "error" not in rounds[-1] else ""
    rec = {"case": case["id"], "expect": case["expect"], "voice": case["voice"], "user": case["text"], "rounds": len(rounds),
           "error": err, "first_any_ms": None, "first_speak_ms": None if first_speak is None else round(first_speak * 1000),
           "first_tool_ms": None if first_tool is None else round(first_tool * 1000),
           "total_ms": round((time.perf_counter() - t_turn0) * 1000), "calls": [{"name": c["name"], "args": c["args"]} for c in calls],
           "spoken": spoken, "final": final, "cost_yuan": round(cost, 6), "guard_cost": guard_cost, "detail": rounds}
    if rounds and "error" not in rounds[0] and rounds[0].get("ttft_any") is not None:
        rec["first_any_ms"] = round(rounds[0]["ttft_any"] * 1000)
    if err is None:
        rec.update(judge(case, calls, spoken, final))
        rec.update(check_rules(spoken, case["voice"]))
        rec["tokens_in"] = sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in rounds)
        rec["tokens_out"] = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in rounds)
        rec["cache_hit"] = sum((r.get("usage") or {}).get("prompt_cache_hit_tokens", 0) or 0 for r in rounds)
        rec["reasoning_tokens"] = sum(((r.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
                                      for r in rounds)
    return rec


# ---------------------------------------------------------------- 子命令


def load_done() -> set[tuple]:
    done = set()
    for f in OUT_DIR.glob("results_*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not r.get("error"):
                done.add((r["key"], r["mode"], r["case"], r["run"]))
    return done


def cmd_run(a: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    key = load_key()
    client = httpx.Client(timeout=httpx.Timeout(90.0, connect=10.0), trust_env=False)
    tools = build_tools()
    global REMINDER
    REMINDER = "\n（语音轮：最多两句，每句不超过30字，纯口语不用符号和括号；要做的事直接调工具。）" if a.reminder else ""
    systems = {m: build_system(m) for m in ("lean", "long")}
    mode_tag = a.mode + ("+rem" if a.reminder else "")
    print(f"system 字符数 lean={len(systems['lean'])} long={len(systems['long'])}；工具数 {len(tools)}", flush=True)
    done = load_done()
    out = OUT_DIR / f"results_{a.tag}.jsonl"
    spent = 0.0
    keys = [k for k in a.keys.split(",") if k]
    cases = [c for c in CASES if a.mode == "lean" or c["id"] in LONG_CASES]
    if a.cases:
        want = set(a.cases.split(","))
        cases = [c for c in cases if c["id"] in want]
    for k in keys:
        mc = MODELS[k]
        n = a.runs or mc.get("n", 2)
        mspent = 0.0
        print(f"=== {k} {mc['id']} mode={a.mode} runs={n}", flush=True)
        for run in range(n):
            for case in cases:
                if (k, mode_tag, case["id"], run) in done:
                    continue
                if spent >= a.budget:
                    print(f"预算保护：本进程估算花费 ¥{spent:.2f} ≥ ¥{a.budget}，停止", flush=True)
                    return
                rec = run_turn(client, key, mc, systems[a.mode], tools, case)
                rec.update({"key": k, "model": mc["id"], "mode": mode_tag, "run": run, "tag": a.tag,
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                spent += rec.get("guard_cost", 0.0)
                mspent += rec.get("guard_cost", 0.0)
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                flag = "ERR " + str(rec["error"])[:60] if rec["error"] else \
                    f"首字 {rec['first_speak_ms']}ms 工具{'✓' if rec['tool_ok'] else '✗'} 规则{'✓' if rec['rules_ok'] else '✗'} {[c['name'] for c in rec['calls']]}"
                print(f"{k:11s} {case['id']:13s} r{run} {flag} ¥{rec['guard_cost']:.4f}", flush=True)
        print(f"--- {k} 本模型估算(不含缓存折扣) ¥{mspent:.3f}；本进程累计 ¥{spent:.3f}", flush=True)
    print(f"完成。本进程估算花费 ¥{spent:.3f}", flush=True)


def real_cost(mc: dict, new_cache: tuple, out_tokens: float) -> float:
    """按线上真实上下文规模估一轮的钱（元）：新输入×未缓存价 + 缓存命中×缓存价 + 输出。"""
    pin, pc, pout = mc["price"]
    return (new_cache[0] * pin + new_cache[1] * pc + out_tokens * pout) / 1e6


def _pct(v: list[float], p: float) -> float | None:
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v) - 1, int(round((len(v) - 1) * p)))]


def cmd_summarize(a: argparse.Namespace) -> None:
    recs = []
    for f in sorted(OUT_DIR.glob("results_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    groups: dict[tuple, list[dict]] = {}
    for r in recs:
        groups.setdefault((r["key"], r["mode"]), []).append(r)
    rows = []
    for (k, mode), rs in groups.items():
        ok = [r for r in rs if not r.get("error")]
        errs = len(rs) - len(ok)
        if not ok:
            rows.append({"key": k, "mode": mode, "n": 0, "errors": errs})
            continue
        mc = MODELS.get(k, {})
        # 撞上限流重试的轮次，延迟里含等待时间，不参与延迟统计（只在 retried 里计数）
        clean = [r for r in ok if not any(d.get("retries", 0) for d in r["detail"])]
        fs = [r["first_speak_ms"] for r in clean if r["first_speak_ms"] is not None]
        fa = [r["first_any_ms"] for r in clean if r["first_any_ms"] is not None]
        direct = [r["first_speak_ms"] for r in clean if r["first_speak_ms"] is not None and not r["calls"]]
        action = [r for r in ok if r["expect"] != "none"]
        cost_turn = st.mean(r["cost_yuan"] for r in ok)
        rows.append({
            "key": k, "model": mc.get("id", ""), "tier": mc.get("tier", ""), "mode": mode, "n": len(ok), "errors": errs,
            "first_token_p50": _pct(fa, .5), "first_speak_p50": _pct(fs, .5), "first_speak_p90": _pct(fs, .9),
            "first_speak_direct_p50": _pct(direct, .5),
            "tool_ok": f"{sum(r['tool_ok'] for r in ok)}/{len(ok)}",
            "tool_ok_action": f"{sum(r['tool_ok'] for r in action)}/{len(action)}",
            "talk_only": sum(r["talk_only"] for r in ok), "fabricated": sum(r["fabricated"] for r in ok),
            "promise": sum(r["promise"] for r in ok),
            "rules_ok": f"{sum(r['rules_ok'] for r in ok)}/{len(ok)}",
            "rules_rate": round(sum(r["rules_ok"] for r in ok) / len(ok), 3),
            "final_ok": f"{sum(r['final_ok'] for r in ok)}/{len(ok)}",
            "tokens_in": round(st.mean(r["tokens_in"] for r in ok)), "tokens_out": round(st.mean(r["tokens_out"] for r in ok)),
            "reasoning_tokens": round(st.mean(r["reasoning_tokens"] for r in ok)),
            "cache_hit": round(st.mean(r["cache_hit"] for r in ok)),
            "yuan_per_turn": round(cost_turn, 5), "yuan_month_bench": round(cost_turn * MONTH_TURNS, 2),
            "yuan_month_real_avg": round(MONTH_TURNS * real_cost(mc, REAL_AVG, st.mean(r["tokens_out"] for r in ok)), 1) if mc else None,
            "yuan_month_real_warm": round(MONTH_TURNS * real_cost(mc, REAL_WARM, st.mean(r["tokens_out"] for r in ok)), 1) if mc else None,
            "retries": sum(d.get("retries", 0) for r in ok for d in r["detail"]),
            "retried_turns": len(ok) - len(clean),
            "never_spoke": sum(1 for r in ok if r["first_speak_ms"] is None),
            "first_speak_max": max(fs) if fs else None,
            "avg_rounds": round(st.mean(r["rounds"] for r in ok), 2),
        })
    (OUT_DIR / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["| 模型 | 模式 | 轮数(撞限流/全程没出声) | 首token p50 | 首个可念字 p50/p90 (ms) | 无工具轮首字 p50 | 调对工具 | 光说不做 | 编造完成 | 规则遵守 | 末答合理 | 入/出 token(平均/轮) | 推理token | ¥/轮 | ¥/月(轻提示) | ¥/月(真实27k上下文) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x["mode"], x["key"])):
        if not r["n"]:
            lines.append(f"| {r['key']} | {r['mode']} | 0 | 全部失败({r['errors']}) ||||||||||||||")
            continue
        lines.append(f"| {r['key']} | {r['mode']} | {r['n']}({r['retried_turns']}/{r['never_spoke']}) | {r['first_token_p50']} | {r['first_speak_p50']}/{r['first_speak_p90']} | "
                     f"{r['first_speak_direct_p50']} | {r['tool_ok']} | {r['talk_only']} | {r['fabricated']} | {r['rules_ok']} | {r['final_ok']} | "
                     f"{r['tokens_in']}/{r['tokens_out']} | {r['reasoning_tokens']} | {r['yuan_per_turn']} | {r['yuan_month_bench']} | {r['yuan_month_real_avg']}（稳前缀 {r['yuan_month_real_warm']}） |")
    # 分用例矩阵：每格 = 通过次数/总次数（工具·规则·末答 三项全过才算过），再附首字中位数
    cases = [c["id"] for c in CASES]
    mat = ["| 模型·模式 | " + " | ".join(cases) + " |", "|---|" + "---|" * len(cases)]
    for (k, mode), rs in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        cells = []
        for cid in cases:
            cr = [r for r in rs if r["case"] == cid and not r.get("error")]
            if not cr:
                cells.append("-")
                continue
            p = sum(1 for r in cr if r["tool_ok"] and r["rules_ok"] and r["final_ok"])
            ms = [r["first_speak_ms"] for r in cr if r["first_speak_ms"] is not None]
            cells.append(f"{p}/{len(cr)} {int(st.median(ms)) if ms else '—'}")
        mat.append(f"| {k}·{mode} | " + " | ".join(cells) + " |")
    (OUT_DIR / "summary.md").write_text("\n".join(lines) + "\n\n分用例（三项全过次数/总次数 + 首个可念字中位数ms）：\n\n" + "\n".join(mat) + "\n",
                                          encoding="utf-8")
    print("\n".join(mat))
    print("\n".join(lines))
    # 按用例看谁没通过
    fail = ["--- 失败明细（工具/规则/末答任一不过）"]
    for r in recs:
        if r.get("error"):
            fail.append(f"{r['key']:11s} {r['mode']:4s} {r['case']:13s} r{r['run']} 请求失败:{str(r['error'])[:80]}")
            continue
        if r["tool_ok"] and r["rules_ok"] and r["final_ok"]:
            continue
        bad = [n for n, v in (("工具", r["tool_ok"]), ("规则", r["rules_ok"]), ("末答", r["final_ok"])) if not v]
        fail.append(f"{r['key']:11s} {r['mode']:4s} {r['case']:13s} r{r['run']} 不过:{','.join(bad)} 句{r['sentences']}/最长{r['max_len']}字 "
                    f"调用:{[c['name'] for c in r['calls']]} 说:{r['spoken'][:70]!r} 末答:{r['final'][:50]!r}")
    (OUT_DIR / "failures.txt").write_text("\n".join(fail) + "\n", encoding="utf-8")


def cmd_aux(a: argparse.Namespace) -> None:
    """顺带测硅基的嵌入 / ASR / TTS 延迟，只用来判断"值不值得替换本地"。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    key = load_key()
    h = {"Authorization": f"Bearer {key}"}
    c = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), trust_env=False)
    out: dict = {}
    # 嵌入（OpenClaw 记忆检索用的 Qwen3-Embedding-0.6B）
    ts = []
    for _ in range(5):
        t0 = time.perf_counter()
        r = c.post(f"{BASE}/embeddings", headers=h, json={"model": "Qwen/Qwen3-Embedding-0.6B", "input": "明天天气怎么样"})
        ts.append(round((time.perf_counter() - t0) * 1000))
        if r.status_code != 200:
            ts[-1] = f"HTTP {r.status_code}"
    out["embedding_ms"] = ts
    # ASR：用 tmp 里现成的录音
    wavs = [p for p in (ROOT / "tmp").glob("bench_t*.wav")][:1] or [ROOT / "tmp" / "mic_check.wav"]
    w = wavs[0]
    try:
        with wave.open(str(w)) as wf:
            dur = wf.getnframes() / wf.getframerate()
    except Exception:
        dur = None
    out["asr_wav"] = {"file": w.name, "seconds": dur}
    for model in ("FunAudioLLM/SenseVoiceSmall", "Qwen/Qwen3-ASR-1.7B"):
        ts, txt = [], ""
        for _ in range(3):
            t0 = time.perf_counter()
            r = c.post(f"{BASE}/audio/transcriptions", headers=h, data={"model": model},
                       files={"file": (w.name, w.read_bytes(), "audio/wav")})
            ms = round((time.perf_counter() - t0) * 1000)
            if r.status_code == 200:
                ts.append(ms)
                txt = r.json().get("text", "")[:60]
            else:
                ts.append(f"HTTP {r.status_code} {r.text[:80]}")
        out[f"asr_{model}"] = {"ms": ts, "text": txt}
    # TTS：首字节时间（流式 PCM）
    text = "好的，我这就去打开网易云音乐。"
    ts = []
    for _ in range(3):
        t0 = time.perf_counter()
        first = None
        total = 0
        try:
            with c.stream("POST", f"{BASE}/audio/speech", headers={**h, "Content-Type": "application/json"},
                          json={"model": "FunAudioLLM/CosyVoice2-0.5B", "input": text,
                                "voice": "FunAudioLLM/CosyVoice2-0.5B:alex", "response_format": "pcm", "sample_rate": 24000,
                                "stream": True}) as r:
                if r.status_code != 200:
                    ts.append(f"HTTP {r.status_code} {r.read()[:120].decode('utf-8', 'replace')}")
                    continue
                for chunk in r.iter_bytes():
                    if chunk and first is None:
                        first = round((time.perf_counter() - t0) * 1000)
                    total += len(chunk)
            ts.append({"first_byte_ms": first, "total_ms": round((time.perf_counter() - t0) * 1000), "bytes": total})
        except httpx.HTTPError as e:
            ts.append(f"{type(e).__name__}")
    out["tts_cosyvoice2"] = {"text": text, "runs": ts}
    (OUT_DIR / "aux.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--keys", required=True, help="逗号分隔：" + ",".join(MODELS))
    r.add_argument("--mode", choices=["lean", "long"], default="lean")
    r.add_argument("--tag", default="x")
    r.add_argument("--reminder", action="store_true", help="语音轮用户消息末尾追加一行规则提醒")
    r.add_argument("--runs", type=int, default=0, help="每用例跑几次；0=按模型默认(2，贵的1)")
    r.add_argument("--cases", default="", help="只跑指定用例，逗号分隔")
    r.add_argument("--budget", type=float, default=3.0, help="本进程估算花费上限（元，不含缓存折扣）")
    r.set_defaults(fn=cmd_run)
    sp.add_parser("summarize").set_defaults(fn=cmd_summarize)
    sp.add_parser("aux").set_defaults(fn=cmd_aux)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
