"""P3 主人微信主线 binding 的 fail-closed 只读配置生成器。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_patch(owner_account_id: str, owner_user_id: str) -> dict:
    if not owner_account_id.strip() or not owner_user_id.strip():
        raise ValueError("必须显式提供 owner account id 与精确大小写 user id")
    return {
        "bindings": [{"agentId": "main", "match": {"channel": "openclaw-weixin", "accountId": owner_account_id},
                      "session": {"dmScope": "main"}}],
        "commands": {"ownerAllowFrom": [owner_user_id], "allowFrom": [owner_user_id]},
        "approvals": {"plugin": {"mode": "both"}},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P3 主人微信 binding 只读 patch 生成器")
    parser.add_argument("--owner-account-id", required=True)
    parser.add_argument("--owner-user-id", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_patch(args.owner_account_id, args.owner_user_id), ensure_ascii=False, indent=2))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
