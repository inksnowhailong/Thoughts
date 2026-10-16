import json
import tempfile
from pathlib import Path

from guanjia.p3_wechat_preflight import inspect_config, latest_inbound_identity
from guanjia.p3_owner_binding import build_patch


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


cfg = {
    "plugins": {"entries": {
        "openclaw-weixin": {"enabled": True},
        "guanjia-guard": {"hooks": {"allowConversationAccess": True}},
    }},
    "bindings": [{"agentId": "main", "match": {"channel": "openclaw-weixin", "accountId": "owner"},
                  "session": {"dmScope": "main"}}],
    "commands": {"ownerAllowFrom": ["Owner"], "allowFrom": ["Owner", "Family"]},
    "approvals": {"plugin": {"mode": "both"}},
}
out = inspect_config(cfg)["checks"]
check(out["wechat_plugin_enabled"] and out["main_wechat_binding"], "plugin and main binding detected")
check(out["owner_allow_configured"] and out["command_allow_configured"], "owner and family command allowlists detected")
check(out["guard_hook_access"] and out["approval_mode"] == "both", "guard hook and approval routing observed")
check("agent:main:main" == out["expected_main_session"], "main session expectation is explicit")
with tempfile.TemporaryDirectory() as directory:
    capture = Path(directory) / "wechat-inbound-identities.jsonl"
    check(latest_inbound_identity(capture) is None, "no inbound event fails closed")
    capture.write_text(json.dumps({"timestamp": "t", "channel": "openclaw-weixin", "accountId": "a",
                                   "senderUserId": "Owner_Exact", "senderSource": "sessionKey",
                                   "sessionCandidate": "agent:main:main"}) + "\n",
                       encoding="utf-8")
    identity = latest_inbound_identity(capture)
    check(identity["senderUserId"] == "Owner_Exact", "latest identity is readable")
    preview = build_patch(identity["accountId"], identity["senderUserId"])
    check(preview["bindings"][0]["match"]["accountId"] == "a" and preview["commands"]["ownerAllowFrom"] == ["Owner_Exact"],
          "binding preview uses captured identity")
    capture.write_text('{"accountId":"a"}\n', encoding="utf-8")
    check(latest_inbound_identity(capture) is None, "incomplete identity fails closed")
print("ALL PASS test_p3_wechat_preflight")
