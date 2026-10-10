"""确认策略 v2 面板契约的参考实现：列出 / 撤销「已记住的放行」。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_perm_remembered.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.remembered_overrides import format_levels, list_remembered, load_levels, revoke, sync_defaults  # noqa: E402

DEFAULTS = ROOT / "openclaw" / "plugins" / "guanjia-guard" / "defaults" / "levels.json"
failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


tmp = Path(tempfile.mkdtemp(prefix="gj-rem-"))
live = tmp / "levels.json"
try:
    # 1. 排版与插件一致：仓库默认表读进来再写回，逐字节相同
    text = DEFAULTS.read_text(encoding="utf-8")
    check(format_levels(json.loads(text)) == text, "format_levels matches plugin layout byte for byte")

    # 2. 一张有三种 overrides 的线上表
    levels = json.loads(text)
    remembered_a = {"id": "rem-aaaaaaaaaaaa", "source": "remembered", "createdAt": "2026-10-10T13:00:00.000Z", "expiresAt": "9999-12-31T00:00:00.000Z",
                    "scope": "global", "tool": "exec", "param": "command", "pattern": "^\\s*shutdown\\s+/s\\s*$", "level": "L1",
                    "summary": "exec: shutdown /s", "reason": "主人口头说以后都允许"}
    remembered_b = dict(remembered_a, id="rem-bbbbbbbbbbbb", createdAt="2026-10-10T12:00:00.000Z", summary="结束进程：leagueclient")
    manual = {"id": "manual-1", "tool": "exec", "param": "command", "pattern": "^python ", "level": "L1", "expiresAt": "2026-12-31T00:00:00.000Z"}
    levels["overrides"] = [remembered_a, manual, remembered_b]
    live.write_text(format_levels(levels), encoding="utf-8")

    items = list_remembered(live)
    check([i["id"] for i in items] == ["rem-bbbbbbbbbbbb", "rem-aaaaaaaaaaaa"], "list_remembered: only source=remembered, oldest first")
    check(items[0]["summary"] == "结束进程：leagueclient", "list_remembered: summary kept (Chinese intact)")

    # 3. 撤销
    check(revoke("manual-1", live) == (False, "not_remembered"), "revoke refuses manual override")
    check(revoke("rem-nope", live) == (False, "not_found"), "revoke unknown id -> not_found")
    check(revoke("rem-aaaaaaaaaaaa", live) == (True, "removed"), "revoke remembered override")
    after = load_levels(live)
    check([o["id"] for o in after["overrides"]] == ["manual-1", "rem-bbbbbbbbbbbb"], "only that entry removed, order kept")
    check(after["rules"] == levels["rules"] and after["tools"] == levels["tools"] and after["defaultLevel"] == "L1", "rules/tools/other keys untouched")
    check(not list(tmp.glob("*.tmp")), "no temp file left behind")
    check(revoke("rem-aaaaaaaaaaaa", live) == (False, "not_found"), "revoke twice -> not_found")

    # 4. 文件坏了：不写、不抛
    before = '{ "broken": '
    live.write_text(before, encoding="utf-8")
    check(revoke("rem-bbbbbbbbbbbb", live) == (False, "unreadable"), "unreadable file -> unreadable")
    check(live.read_text(encoding="utf-8") == before, "unreadable file left as is")
    try:
        list_remembered(live)
        check(False, "list_remembered raises on unreadable file")
    except ValueError:
        check(True, "list_remembered raises on unreadable file")

    # 5. 带 BOM 的文件（主人用记事本另存过）也能读
    live.write_bytes("\ufeff".encode("utf-8") + format_levels(levels).encode("utf-8"))
    check(len(list_remembered(live)) == 2, "BOM file readable")

    # 6. 上线同步：换成仓库默认表，线上 overrides 全部保留
    old = dict(levels, defaultLevel="L2", rules=[], overrides=[remembered_a, manual])
    live.write_text(format_levels(old), encoding="utf-8")
    check(sync_defaults(DEFAULTS, live) == 2, "sync_defaults keeps both overrides")
    synced = load_levels(live)
    check(synced["defaultLevel"] == "L1" and synced["rules"] == levels["rules"], "sync_defaults takes rules/tools from repo defaults")
    check([o["id"] for o in synced["overrides"]] == ["rem-aaaaaaaaaaaa", "manual-1"], "sync_defaults keeps overrides in order")
    fresh = tmp / "fresh.json"
    check(sync_defaults(DEFAULTS, fresh) == 0 and fresh.read_text(encoding="utf-8") == text, "no live file -> exact copy of defaults")
    live.write_text("not json", encoding="utf-8")
    try:
        sync_defaults(DEFAULTS, live)
        check(False, "sync_defaults refuses unreadable live file")
    except ValueError:
        check(live.read_text(encoding="utf-8") == "not json", "sync_defaults refuses unreadable live file")

    # 7. 命令行：list / revoke（验收时面板还没做，用它撤销）
    import subprocess
    live.write_text(format_levels(dict(levels, overrides=[remembered_a, manual])), encoding="utf-8")
    run = lambda *a: subprocess.run([sys.executable, "-m", "guanjia.remembered_overrides", *a], capture_output=True, text=True, encoding="utf-8",
                                    env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"})
    out = run("list", str(live))
    check(out.returncode == 0 and json.loads(out.stdout.strip())["id"] == "rem-aaaaaaaaaaaa", "cli list prints remembered as JSON lines")
    out = run("revoke", "manual-1", str(live))
    check(out.returncode == 1 and "not_remembered" in out.stdout, "cli revoke refuses manual entry")
    out = run("revoke", "rem-aaaaaaaaaaaa", str(live))
    check(out.returncode == 0 and "removed" in out.stdout and not list_remembered(live), "cli revoke removes remembered entry")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS test_perm_remembered" if failed == 0 else f"FAILED {failed}")
sys.exit(1 if failed else 0)
