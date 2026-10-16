"""P3 主人微信主线 binding 的 fail-closed patch 构造。"""

from __future__ import annotations


def build_patch(owner_account_id: str, owner_user_id: str) -> dict:
    if not owner_account_id.strip() or not owner_user_id.strip():
        raise ValueError("必须显式提供 owner account id 与精确大小写 user id")
    return {
        "bindings": [{"agentId": "main", "match": {"channel": "openclaw-weixin", "accountId": owner_account_id},
                      "session": {"dmScope": "main"}}],
        "commands": {"ownerAllowFrom": [owner_user_id], "allowFrom": [owner_user_id]},
        "approvals": {"plugin": {"mode": "both"}},
    }
