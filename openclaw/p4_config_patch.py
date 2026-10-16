"""P4 配置补丁：P4-1 瘦身与 P4-1b 直接记忆工具面（在家里电脑上跑；先读一遍再 --apply）。

用法（不带模式什么都不做）：
  python openclaw\\p4_config_patch.py --check      # 只看会改什么、预计省多少 token；不写任何文件
  python openclaw\\p4_config_patch.py --apply      # 备份为 openclaw.json.bak-p4-1 后写入；网关必须先停
  python openclaw\\p4_config_patch.py --rollback   # 把备份拷回去；同样要网关停着
  python openclaw\\p4_config_patch.py --step 1b --check     # 只核对 P4-1b；不写任何文件
  python openclaw\\p4_config_patch.py --step 1b --apply     # 备份为 openclaw.json.bak-p4-1b 后写入；网关必须先停
  python openclaw\\p4_config_patch.py --step 1b --rollback  # 只回滚 P4-1b，保留 P4-1
  --file <路径>      指向别的 openclaw.json（测试用；此时不查网关端口、不调 openclaw CLI）
  --baseline <json>  prompt_size.py 生成的基线（默认取 tmp\\prompt-size 里最新的），--check 用它估省多少 token
  --force            网关还在监听也硬改（不建议）

流程：停网关（openclaw gateway stop --force，确认 18789 不监听）→ --apply → 启网关 → 跑 scripts\\prompt_size.py 复测；
出问题 → 停网关 → --rollback → 启网关。本脚本不启停任何东西。
线上写入前后各校验一次 schema：写前用 `openclaw config patch --dry-run`，写后用 `openclaw config validate`，写后不过自动恢复备份。

== 去掉的东西（每项：是什么 / 为什么用不到 / 省多少 / 风险；token 为 2026-10-13 基线的估算，实测约高 9%）==
见下面 CHANGES；--check 也会打印同样的清单和按最新基线算的数字。

== 刻意不动的（拿不准或与记忆检索有关）==
- 记忆：memory.search（embedding 配置）、memory_search / memory_get 工具、memory-core 插件及其定时任务、hooks（换新摘录条数先不改）。
- tool_search / tool_describe / tool_call：memory_search、memory_get、automations（提醒）、web_fetch、browser 都藏在这个目录后面，关掉就够不着。
- process：exec 超过前台等待时间会转后台，模型要靠 process 取结果；裁掉省 ≈0.4k token，但长命令会变成「没有结果」。
- 压缩（compaction）：沿用默认，P4 计划第 7 节说明稳定一周后再评估。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
BACKUP_SUFFIX = ".bak-p4-1"
P4_1B_BACKUP_SUFFIX = ".bak-p4-1b"
GATEWAY_PORT = 18789
NODE = Path.home() / "AppData/Roaming/fnm/node-versions/v24.21.0/installation/node.exe"
OPENCLAW_MJS = NODE.parent / "node_modules" / "openclaw" / "openclaw.mjs"

# 主线只保留这些技能（名字以 systemPromptReport.skills.entries 为准）：
#   guanjia-launch / guanjia-lookup / guanjia-notes：我们自己的三个；
#   weather：与 guanjia-lookup 的 get_weather 有重叠，但 P4 计划里定了留，观察后再删；
#   windows-host-tasks：工作坊自己长出来的技能，管「在可见桌面开程序、开关 Clash 代理」，有真实用途；
#   browser-automation：guanjia-launch「找片」要用 browser 工具开页面，这个技能是它的操作要领；
#   skill-creator：技能工作坊每周复盘 / 自学会碰技能文件，拿不准，留。
SKILLS_KEEP = ["guanjia-launch", "guanjia-lookup", "guanjia-notes", "weather", "windows-host-tasks", "browser-automation", "skill-creator"]
# 主线直接可见工具里明显用不到的三个
DENY_TOOLS = ["apply_patch", "openclaw", "sessions_yield"]
# P4-1b：关闭 Tool Search 后仅保留现有常用能力与设计指定的记忆检索能力。
# 该白名单只改变工具面，不扩大既有目录中的权限；process 明确保留。
P4_1B_ALLOW_TOOLS = [
    "exec", "process", "read", "write", "edit", "ls",
    "web_fetch", "web_search", "browser", "automations", "intent", "session_status",
    "memory_search", "memory_get", "sessions_search", "sessions_history",
    "speak", "launch_app", "get_location", "get_weather", "get_desktop_status",
    "task_start", "task_status", "task_cancel", "task_report",
]
TASK_TOOLS = ["task_start", "task_status", "task_cancel", "task_report"]

CHANGES = [
    {
        "id": "heartbeat",
        "what": "agents.defaults.heartbeat.every = \"0m\"：关掉 OpenClaw 每 30 分钟对主线会话跑一次的心跳（cron 里的 heartbeat-main）。",
        "why": "管家不靠心跳干活（提醒走 automations，定时任务走我们自己的 cron）。现在每次都是 skipped:no-route，0.3 秒、不调模型；"
               "但一旦配好微信路由（P3），心跳会变成每 30 分钟一整轮主线对话，还会改写主线的最近路由。",
        "saves": "眼下 0 token（预防项）；有路由后 ≈ 每天 48 轮整轮开销",
        "risk": "极低。目标事件唤醒（如后台 exec 完成）按文档仍可跑一轮。重启后 `openclaw cron list` 里 heartbeat-main 应消失或停用，仍在就跑 `openclaw doctor --fix`。",
    },
    {
        "id": "session-reset",
        "what": "session.reset = {mode: \"none\"}：显式钉住「不自动换新」。",
        "why": "换新交给 P4 休眠；OpenClaw 默认本来就是 none（线上主线会话已连续 30 小时未重置，实测），所以现在是零变化的保险，防止以后升级改默认、或有人加 daily/idle。",
        "saves": "0 token（保险项）",
        "risk": "无。/new、/reset 仍可手动用。",
    },
    {
        "id": "skills",
        "what": "agents.entries.main.skills = 白名单（见 SKILLS_KEEP，7 个）：主线技能列表从 25 个减到 7 个。去掉 18 个：add-model-provider、canvas、clawhub、"
                "cloud-image-bake、configure-channel、control-ui、diagnose-gateway、diagram-maker、healthcheck、meme-maker、node-connect、"
                "node-inspect-debugger、notion、python-debugpy、spike、taskflow、taskflow-inbox-triage、visualize。",
        "why": "都是 OpenClaw 自带的运维 / 开发 / 作图 / 外部服务技能：管家是家庭语音助手，没有这些场景（没配 Notion、没有 canvas/控制台/节点连接、不改模型与渠道、不调试代码）。"
               "每个技能的名字+描述+路径每轮都占 80~100 token。clawhub 还能让模型联网装新技能，去掉也更稳。",
        "saves": "≈1.6k token / 轮（按基线逐项相加）",
        "risk": "白名单名字写错 → 该技能从列表消失（--check 会对照基线报告不存在的名字）；工作坊以后新长出的技能不在名单里就看不到，需要时往 SKILLS_KEEP 加。"
                "白名单只是可见性过滤，不是安全边界。主线会话的技能快照可能缓存，验证以 prompt_size.py 的新会话为准。",
    },
    {
        "id": "tools-deny",
        "what": "agents.entries.main.tools.deny += apply_patch、openclaw、sessions_yield（合并，不覆盖已有 deny）。",
        "why": "apply_patch：已有 edit / write，管家改文件只做小改动；openclaw：「委托 OpenClaw 做安装与修复」，管家不该也不需要自己改自己；"
               "sessions_yield：只给「派子代理后让出本轮」用，管家不派子代理。",
        "saves": "≈0.4k token / 轮（摘要 + schema）",
        "risk": "低。模型若想用这三个会得到「工具不可用」，线上 AGENTS / SOUL / IDENTITY / USER 与 guanjia-launch / lookup / notes、windows-host-tasks 里没有引用它们（已核对）。",
    },
]

P4_1B_CHANGES = [
    {
        "id": "tool-search",
        "what": "tools.toolSearch = false：恢复白名单工具的直接 schema。",
        "why": "memory_search / memory_get 不再需要先经 tool_search 才能调用。",
        "risk": "直接 schema 会增加固定提示词体积；上线窗口用 prompt_size.py 复测。",
    },
    {
        "id": "tools-allow",
        "what": "agents.entries.main.tools.allow = P4_1B_ALLOW_TOOLS。",
        "why": "防止关闭 Tool Search 后暴露未审查工具；process 保留，现有 deny 原样保留。",
        "risk": "白名单遗漏会让对应工具不可用；上线后的 inspect 用于核实。",
    },
    {
        "id": "dreaming",
        "what": "plugins.entries.memory-core.config.dreaming.enabled = false。",
        "why": "MEMORY.md 只由后续 P4 休眠整理器写入，避免双写者。",
        "risk": "停止 memory-core 托管的 dreaming sweep；显式 rollback 可恢复原状。",
    },
]


def apply(cfg: dict) -> dict:
    """返回打过补丁的新配置（不改入参，幂等）。只动 agents.defaults.heartbeat.every、agents.entries.main.{skills,tools.deny}、session.reset.mode。"""
    new = copy.deepcopy(cfg)
    agents = new.setdefault("agents", {})
    agents.setdefault("defaults", {}).setdefault("heartbeat", {})["every"] = "0m"
    main = agents.setdefault("entries", {}).setdefault("main", {})
    main["skills"] = list(SKILLS_KEEP)
    tools = main.setdefault("tools", {})
    deny = list(tools.get("deny") or [])
    tools["deny"] = deny + [t for t in DENY_TOOLS if t not in deny]
    new.setdefault("session", {}).setdefault("reset", {})["mode"] = "none"
    return new


def apply_1b(cfg: dict) -> dict:
    """只应用 P4-1b：保留已部署 allow 的未知手工项，只补四个受审查 task 工具。"""
    new = copy.deepcopy(cfg)
    new.setdefault("tools", {})["toolSearch"] = False
    main_tools = new.setdefault("agents", {}).setdefault("entries", {}).setdefault("main", {}).setdefault("tools", {})
    current = list(main_tools.get("allow") or [])
    for tool in P4_1B_ALLOW_TOOLS + TASK_TOOLS:
        if tool not in current:
            current.append(tool)
    main_tools["allow"] = current
    new.setdefault("plugins", {}).setdefault("entries", {}).setdefault("memory-core", {}).setdefault("config", {}).setdefault("dreaming", {})["enabled"] = False
    return new


def patch_object(old: dict, new: dict) -> dict:
    """只含改动子树的补丁对象（对象递归合并、数组/标量整体替换，与 `openclaw config patch` 的语义一致）。"""
    out: dict = {}
    for k, v in new.items():
        if k not in old:
            out[k] = v
        elif isinstance(v, dict) and isinstance(old[k], dict):
            sub = patch_object(old[k], v)
            if sub:
                out[k] = sub
        elif v != old[k]:
            out[k] = v
    return out


def flatten(obj: dict, prefix: str = "") -> list[tuple[str, object]]:
    """补丁对象摊平成 (点路径, 新值)，数组算叶子。"""
    items: list[tuple[str, object]] = []
    for k, v in obj.items():
        if isinstance(v, dict):
            items += flatten(v, f"{prefix}{k}.")
        else:
            items.append((f"{prefix}{k}", v))
    return items


def saving(baseline: dict, new_cfg: dict) -> dict:
    """按基线里每个技能 / 工具的估算 token，算补丁后去掉了谁、省多少；基线里没有的名字单列（多半是写错了）。"""
    bd = baseline.get("breakdown") or {}
    skills, tools = bd.get("skills") or {}, bd.get("tools") or {}
    main = new_cfg["agents"]["entries"]["main"]
    keep, deny = main["skills"], main["tools"]["deny"]
    s_gone = [n for n in skills if n not in keep]
    t_gone = [n for n in tools if n in deny]
    return {
        "skills_removed": s_gone, "skills_tokens": sum(skills[n] for n in s_gone),
        "tools_removed": t_gone, "tools_tokens": sum(tools[n] for n in t_gone),
        "unknown_keep": [n for n in keep if n not in skills], "unknown_deny": [n for n in deny if n not in tools],
        "before": baseline.get("median_prompt_tokens"),
    }


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def latest_baseline() -> Path | None:
    files = sorted((ROOT / "tmp" / "prompt-size").glob("*.json"))
    return files[-1] if files else None


def dump(cfg: dict) -> str:
    return json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"


def openclaw_cli(*args: str) -> subprocess.CompletedProcess:
    """调 openclaw CLI（argv 直传，不经 shell）。"""
    return subprocess.run([str(NODE), str(OPENCLAW_MJS), *args], capture_output=True, text=True, encoding="utf-8", timeout=120)


def dry_run_validate(patch_obj: dict) -> tuple[bool, str]:
    """用 `openclaw config patch --dry-run` 按线上 schema 校验补丁（不写任何配置）。"""
    with tempfile.TemporaryDirectory(prefix="gj-p4patch-") as d:
        p = Path(d) / "patch.json"
        p.write_text(json.dumps(patch_obj, ensure_ascii=False), encoding="utf-8")
        r = openclaw_cli("config", "patch", "--file", str(p), "--dry-run")
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[-400:]


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp-p4")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def print_ledger(sv: dict | None, patch_obj: dict, *, step: str) -> None:
    changes = CHANGES if step == "1" else P4_1B_CHANGES
    for c in changes:
        print(f"[{c['id']}]\n  是什么：{c['what']}\n  为什么：{c['why']}")
        if step == "1":
            print(f"  省多少：{c['saves']}")
        print(f"  风险：{c['risk']}\n")
    print("将写入的键（旧 → 新由补丁对象决定，其余逐字节保留）：")
    for path, val in flatten(patch_obj):
        print(f"  {path} = {json.dumps(val, ensure_ascii=False)}")
    if step == "1b":
        print("\nP4-1b 保护：process 不在 deny；既有 deny 和 allow 的未知手工项均不覆盖；只追加缺失 task 工具。")
        return
    if not sv:
        print("\n（没有基线：先跑 scripts\\prompt_size.py 再 --check，才能算省多少 token）")
        return
    print(f"\n按基线估算：去掉技能 {len(sv['skills_removed'])} 个 ≈{sv['skills_tokens']} token，去掉工具 {len(sv['tools_removed'])} 个 ≈{sv['tools_tokens']} token，"
          f"合计 ≈{sv['skills_tokens'] + sv['tools_tokens']}（估算口径比实测约低 9%，实际省量略大）")
    if sv["before"]:
        print(f"每轮 prompt_tokens：{sv['before']:g} → ≈{sv['before'] - (sv['skills_tokens'] + sv['tools_tokens']) * 1.09:.0f}")
    if sv["unknown_keep"] or sv["unknown_deny"]:
        print(f"警告：基线里没有这些名字（写错了？或基线过期）：技能 {sv['unknown_keep']} 工具 {sv['unknown_deny']}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    mode = next((m for m in ("--check", "--apply", "--rollback") if m in args), None)
    if not mode:
        print("用法：python p4_config_patch.py [--step 1b] --check | --apply | --rollback [--file <openclaw.json>] [--baseline <json>] [--force]（详见文件头注释）")
        return 2

    def value(flag: str) -> str | None:
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None

    step_arg = value("--step")
    if step_arg not in (None, "1b"):
        print(f"未知步骤 {step_arg!r}；只支持默认 P4-1 或 --step 1b。")
        return 2
    step = step_arg or "1"
    target = Path(value("--file") or LIVE_CONFIG)
    live = value("--file") is None
    backup = target.with_name(target.name + (P4_1B_BACKUP_SUFFIX if step == "1b" else BACKUP_SUFFIX))
    if mode != "--check" and live and port_open(GATEWAY_PORT) and "--force" not in args:
        print(f"网关还在监听 {GATEWAY_PORT}：先 openclaw gateway stop --force，确认不监听再来（或加 --force，不建议）。")
        return 1

    if mode == "--rollback":
        if not backup.exists():
            print(f"没有备份 {backup}，无从回滚。")
            return 1
        atomic_write(target, backup.read_bytes())
        print(f"已恢复 {target} <- {backup.name}。重启网关生效。")
        return 0

    raw = target.read_bytes()
    cfg = json.loads(raw.decode("utf-8"))
    if dump(cfg).encode("utf-8") != raw:
        print(f"{target} 不是 2 空格缩进 + 末尾换行的标准 JSON，重写会改动无关格式：拒绝。请先人工确认后手改。")
        return 1
    new = apply_1b(cfg) if step == "1b" else apply(cfg)
    po = patch_object(cfg, new)
    if mode == "--check":
        bl = Path(value("--baseline")) if value("--baseline") else latest_baseline()
        sv = saving(json.loads(bl.read_text(encoding="utf-8")), new) if step == "1" and bl and bl.exists() else None
        print(f"== P4-{step} --check {target}（不写任何文件；基线：{bl.name if sv else '不适用'}）==\n")
        print_ledger(sv, po, step=step)
        if not po:
            print(f"\nP4-{step} 配置已经是补丁后的样子，无需改动。")
        elif live:
            ok, msg = dry_run_validate(po)
            print(f"\nopenclaw config patch --dry-run：{'通过' if ok else '失败'} {msg}")
            return 0 if ok else 1
        else:
            print("\n（--file：跳过 openclaw CLI 校验）")
        return 0

    if not po:
        print("配置已经是补丁后的样子，无需改动。")
        return 0
    if live:
        ok, msg = dry_run_validate(po)
        if not ok:
            print(f"schema 校验失败，没有写任何东西：{msg}")
            return 1
    if backup.exists() and backup.read_bytes() != raw:  # 旧备份不是当前这份原件：留档改名，别混用
        shutil.move(backup, backup.with_name(f"{backup.name}.{time.strftime('%Y%m%d-%H%M%S')}"))
    if not backup.exists():
        shutil.copy2(target, backup)
    atomic_write(target, dump(new).encode("utf-8"))
    if live:
        r = openclaw_cli("config", "validate")
        if r.returncode != 0:
            atomic_write(target, backup.read_bytes())
            print(f"写入后 config validate 失败，已恢复备份：{((r.stdout or '') + (r.stderr or ''))[-300:]}")
            return 1
    rollback = "python p4_config_patch.py --step 1b --rollback" if step == "1b" else "python p4_config_patch.py --rollback"
    print(f"已写入 {target}（备份 {backup.name}）。改动：{', '.join(p for p, _ in flatten(po))}\n重启网关生效；回滚：{rollback}（网关停着时）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
