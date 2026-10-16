"""P3 微信真机前只读检查：不连微信、不发消息、不改绑定。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from guanjia.p3_owner_binding import build_patch
from guanjia.p3_wechat_preflight import inspect_config, latest_inbound_identity

DEFAULT_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_IDENTITIES = Path.home() / ".openclaw" / "workspace" / "guanjia" / "wechat-inbound-identities.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P3 微信只读 preflight")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--identity-file", type=Path, default=DEFAULT_IDENTITIES)
    args = parser.parse_args(argv)
    try:
        cfg = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"配置不可读：{type(exc).__name__}"}, ensure_ascii=False))
        return 2
    identity = latest_inbound_identity(args.identity_file)
    preview = (build_patch(identity["accountId"], identity["senderUserId"]) if identity else None)
    print(json.dumps({"ok": True, **inspect_config(cfg), "latest_inbound_identity": identity,
                      "identity_capture_ready": identity is not None, "binding_preview": preview,
                      "binding_preview_notice": "仅供人工审查，脚本不会写入配置或 apply"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
