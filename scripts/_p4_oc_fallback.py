"""P4-0 临时兜底：OpenClaw 数据库路径与网关 RPC 调用。P4-3 的 guanjia.sleep.oc 合并后删除本文件。
只放 AGENT_DB / NODE / OPENCLAW_MJS / gateway_call 四样，代码与 P4-3 oc.py 保持一致。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

AGENT_DB = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
NODE = Path.home() / "AppData/Roaming/fnm/node-versions/v24.21.0/installation/node.exe"
OPENCLAW_MJS = NODE.parent / "node_modules" / "openclaw" / "openclaw.mjs"


def gateway_call(method: str, params: dict, timeout: float = 30.0) -> dict:
    """调网关 RPC：argv 直传（不经 shell），JSON 参数不会被 PowerShell 吞引号；输出里横幅之后第一个 { 起是 JSON。"""
    p = subprocess.run([str(NODE), str(OPENCLAW_MJS), "gateway", "call", method, "--params", json.dumps(params), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise RuntimeError(f"{method} rc={p.returncode}: {out[-400:]}")
    text = p.stdout or "{}"
    return json.loads(text[text.index("{"):]) if "{" in text else {}
