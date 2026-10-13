"""P4-1：配置瘦身补丁（openclaw/p4_config_patch.py）。只用临时目录里的假 openclaw.json，绝不碰线上配置。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p4_config_patch.py
"""

from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "openclaw"))

import p4_config_patch as patch  # noqa: E402

failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


# 仿线上结构的假配置（密钥是假的）；与线上一样：2 空格缩进、末尾换行、agents.entries.main 为空对象
CFG = {
    "agents": {"defaults": {"model": {"primary": "p/x", "fallbacks": ["p/y"]}, "models": {"p/y": {"params": {"k": 1}}}},
               "entries": {"main": {}}},
    "models": {"mode": "merge", "providers": {"p": {"baseUrl": "https://x/v1", "apiKey": "FAKE"}}},
    "plugins": {"entries": {"guanjia-guard": {"enabled": True}}},
    "skills": {"entries": {"github": {"enabled": False}}},
    "gateway": {"mode": "local", "auth": {"mode": "token", "token": "FAKE"}},
    "session": {"dmScope": "per-account-channel-peer"},
    "memory": {"search": {"provider": "openai", "model": "Qwen/Qwen3-Embedding-0.6B"}},
    "approvals": {},
}
TEXT = json.dumps(CFG, ensure_ascii=False, indent=2) + "\n"

KEEP = ["guanjia-launch", "guanjia-lookup", "guanjia-notes", "weather", "windows-host-tasks", "browser-automation", "skill-creator"]
DENY = {"apply_patch", "openclaw", "sessions_yield"}

# ---- 纯函数 apply ----
c2 = patch.apply(CFG)
main = c2["agents"]["entries"]["main"]
check(main["skills"] == KEEP, "skills allowlist is exactly the reviewed list")
check(set(main["tools"]["deny"]) == DENY, "tools deny is exactly the three unused tools")
check(not any(n.startswith(("memory", "tool_", "automations", "web_", "exec", "process", "browser")) for n in main["tools"]["deny"]),
      "deny never touches memory / tool_search / automations / web / exec / process / browser")
check(c2["agents"]["defaults"]["heartbeat"] == {"every": "0m"}, "heartbeat off")
check(c2["session"] == {"dmScope": "per-account-channel-peer", "reset": {"mode": "none"}}, "reset pinned to none, dmScope kept")
for k in ("models", "plugins", "skills", "gateway", "memory", "approvals"):
    check(c2[k] == CFG[k], f"section untouched: {k}")
check(c2["agents"]["defaults"]["model"] == CFG["agents"]["defaults"]["model"] and c2["agents"]["defaults"]["models"] == CFG["agents"]["defaults"]["models"],
      "agents.defaults model settings untouched")
check("hooks" not in c2, "hooks (session-memory excerpt count) not added in this batch")
check(CFG["agents"]["entries"]["main"] == {}, "apply does not mutate its input")
check(patch.apply(c2) == c2, "idempotent")

pre = copy.deepcopy(CFG)
pre["agents"]["entries"]["main"] = {"tools": {"deny": ["canvas", "openclaw"]}, "model": "keep-me"}
pre["agents"]["defaults"]["heartbeat"] = {"every": "30m", "target": "none"}
c3 = patch.apply(pre)
check(c3["agents"]["entries"]["main"]["tools"]["deny"] == ["canvas", "openclaw", "apply_patch", "sessions_yield"], "existing deny entries kept, new ones appended once")
check(c3["agents"]["entries"]["main"]["model"] == "keep-me" and c3["agents"]["defaults"]["heartbeat"] == {"every": "0m", "target": "none"},
      "sibling keys under main / heartbeat kept")

# ---- patch_object：只含改动子树（交给 openclaw config patch --dry-run 校验用） ----
po = patch.patch_object(CFG, c2)
check(set(po) == {"agents", "session"} and po["session"] == {"reset": {"mode": "none"}}, "patch object has only changed subtrees")
check(po["agents"]["entries"]["main"]["skills"] == KEEP and "model" not in po["agents"]["defaults"], "patch object subtree is minimal")

# ---- 省 token 估算：基线 JSON 里的 skills / tools 估算值 ----
base = {"median_prompt_tokens": 1000, "breakdown": {"skills": {"guanjia-launch": 70, "weather": 90, "canvas": 66, "spike": 80},
                                                    "tools": {"exec": 500, "apply_patch": 60, "openclaw": 140}}}
sv = patch.saving(base, c2)
check(sv["skills_removed"] == ["canvas", "spike"] and sv["skills_tokens"] == 146, "saving: removed skills and tokens")
check(sv["tools_removed"] == ["apply_patch", "openclaw"] and sv["tools_tokens"] == 200, "saving: removed tools and tokens")
check(sv["unknown_deny"] == ["sessions_yield"] and "guanjia-notes" in sv["unknown_keep"], "saving: names missing from baseline are flagged")

# ---- 端口探测 ----
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
check(patch.port_open(port) is True, "port_open sees a listener")
srv.close()
check(patch.port_open(port) is False, "port_open sees a closed port")

# ---- CLI（--file 指向临时副本；不带 --file 的线上路径在测试里永远不会被用到） ----
tmp = Path(tempfile.mkdtemp(prefix="gj-p4cfg-"))
f = tmp / "openclaw.json"
f.write_bytes(TEXT.encode("utf-8"))
bak = tmp / "openclaw.json.bak-p4-1"
SCRIPT = str(ROOT / "openclaw" / "p4_config_patch.py")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", SCRIPT, *args], capture_output=True, text=True, encoding="utf-8", timeout=60)


r = run("--file", str(f))
check(r.returncode == 2 and f.read_bytes() == TEXT.encode("utf-8"), "no mode: usage, exit 2, nothing written")
r = run("--check", "--file", str(f))
check(r.returncode == 0 and f.read_bytes() == TEXT.encode("utf-8") and not bak.exists(), "--check writes nothing (no backup either)")
check("heartbeat" in r.stdout and "skills" in r.stdout and "apply_patch" in r.stdout, "--check prints the change list")
r = run("--rollback", "--file", str(f))
check(r.returncode != 0 and f.read_bytes() == TEXT.encode("utf-8"), "--rollback without backup fails and writes nothing")
r = run("--apply", "--file", str(f))
check(r.returncode == 0, f"--apply ok ({r.stderr[-200:]})")
check(bak.read_bytes() == TEXT.encode("utf-8"), "backup is the original, byte for byte")
new_text = f.read_text(encoding="utf-8")
check(json.loads(new_text) == c2 and new_text == json.dumps(c2, ensure_ascii=False, indent=2) + "\n", "patched file == apply(original), same formatting")
r = run("--apply", "--file", str(f))
check(r.returncode == 0 and "无需改动" in r.stdout and bak.read_bytes() == TEXT.encode("utf-8") and f.read_text(encoding="utf-8") == new_text,
      "second --apply is a no-op and keeps the original backup")
r = run("--rollback", "--file", str(f))
check(r.returncode == 0 and f.read_bytes() == TEXT.encode("utf-8"), "--rollback restores original byte for byte")

odd = tmp / "odd.json"
odd.write_bytes(b'{\n\t"agents": {"entries": {"main": {}}}\n}\n')  # 制表符缩进：重写会改格式，拒绝
r = run("--apply", "--file", str(odd))
check(r.returncode != 0 and odd.read_bytes() == b'{\n\t"agents": {"entries": {"main": {}}}\n}\n', "refuses to rewrite a file it cannot round-trip")

sys.exit(1 if failed else 0)
