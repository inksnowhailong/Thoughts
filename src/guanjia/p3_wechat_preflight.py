"""P3 微信真机前只读配置检查。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def latest_inbound_identity(path: Path) -> dict[str, Any] | None:
    """仅读取最后一条完整元数据；缺失、损坏或字段不全均 fail-closed。"""
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        record = json.loads(lines[-1])
    except (OSError, ValueError, IndexError):
        return None
    expected = {"timestamp", "channel", "accountId", "senderUserId", "senderSource", "sessionCandidate"}
    if not isinstance(record, dict) or set(record) != expected or record.get("channel") != "openclaw-weixin":
        return None
    if not all(isinstance(record[key], str) and record[key] for key in expected):
        return None
    if record["senderSource"] not in {"context", "sessionKey"}:
        return None
    return record


def inspect_config(cfg: dict[str, Any]) -> dict[str, Any]:
    plugins = ((cfg.get("plugins") or {}).get("entries") or {})
    wechat = {name: item for name, item in plugins.items() if "weixin" in name.lower() or "wechat" in name.lower()}
    guard = plugins.get("guanjia-guard") or {}
    bindings = cfg.get("bindings") or []
    main_bindings = [item for item in bindings if isinstance(item, dict)
                     and (item.get("match") or {}).get("channel") == "openclaw-weixin"
                     and (item.get("session") or {}).get("dmScope") == "main"]
    commands = cfg.get("commands") or {}
    approvals = ((cfg.get("approvals") or {}).get("plugin") or {})
    return {"checks": {
        "wechat_plugin_configured": bool(wechat),
        "wechat_plugin_enabled": any(isinstance(item, dict) and item.get("enabled") is not False for item in wechat.values()),
        "main_wechat_binding": bool(main_bindings),
        "owner_allow_configured": bool(commands.get("ownerAllowFrom")),
        "command_allow_configured": bool(commands.get("allowFrom")),
        "approval_mode": approvals.get("mode"),
        "guard_hook_access": bool((guard.get("hooks") or {}).get("allowConversationAccess")),
        "main_watermark_path": str(Path.home() / ".openclaw" / "workspace" / "guanjia" / "main-watermark.json"),
        "expected_main_session": "agent:main:main",
        "expected_family_session_prefix": "agent:main:openclaw-weixin:<account>:direct:<peer>",
        "reset_delivery_observation": "reset 后检查插件路由键、sessionId、未消费注入和 delivery/lastRoute",
        "confirmation_observation": "确认类消息检查 ctx.sessionKey、requester.channel/accountId、审批 targets/session",
    }, "wechat_plugin_ids": sorted(wechat), "main_binding_count": len(main_bindings)}


def load_and_inspect(path: Path) -> dict[str, Any]:
    return inspect_config(json.loads(path.read_text(encoding="utf-8")))
