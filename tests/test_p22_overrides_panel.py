"""P2.2 B4：面板「已记住的放行」列表与撤销（路由层，用临时 levels.json，不碰线上文件）。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_overrides_panel.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.events import EventHub  # noqa: E402
from guanjia.panel import PANEL_HTML, PanelBackend  # noqa: E402
from guanjia.remembered_overrides import format_levels  # noqa: E402
from guanjia.settings import SettingsStore  # noqa: E402

DEFAULTS = ROOT / "openclaw" / "plugins" / "guanjia-guard" / "defaults" / "levels.json"
failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


class FakeLog:
    """记录 emit 调用，验证写操作进了统一事件日志。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, comp, ev, msg="", **kw):
        self.events.append((comp, ev, msg, kw))


tmp = Path(tempfile.mkdtemp(prefix="gj-ovp-"))
try:
    live = tmp / "levels.json"
    hist = tmp / "history"
    levels = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    rem_a = {"id": "rem-aaaaaaaaaaaa", "source": "remembered", "createdAt": "2026-10-10T13:00:00.000Z", "expiresAt": "9999-12-31T00:00:00.000Z",
             "scope": "global", "tool": "exec", "param": "command", "pattern": "^\\s*shutdown\\s+/s\\s*$", "level": "L1",
             "summary": "exec: shutdown /s", "reason": "主人口头说以后都允许"}
    rem_b = dict(rem_a, id="rem-bbbbbbbbbbbb", createdAt="2026-10-10T12:00:00.000Z", summary="结束进程：leagueclient")
    manual = {"id": "manual-1", "tool": "exec", "param": "command", "pattern": "^python ", "level": "L1", "expiresAt": "2026-12-31T00:00:00.000Z"}
    levels["overrides"] = [rem_a, manual, rem_b]
    live.write_text(format_levels(levels), encoding="utf-8")

    panel = PanelBackend(EventHub(), SettingsStore(None), status=lambda: {}, control=lambda a: (True, a),
                         inject=lambda t, d: {}, token="t", levels_path=live, audit_dir=hist)
    log = FakeLog()
    panel._elog = log

    # 1. 列表：只有 remembered 两条，按记住的先后
    st, body = panel.route_get("/api/overrides", {})
    check(st == 200 and [i["id"] for i in body["items"]] == ["rem-bbbbbbbbbbbb", "rem-aaaaaaaaaaaa"], "GET lists only remembered, oldest first")
    check(body["items"][0]["summary"] == "结束进程：leagueclient", "GET keeps Chinese summary")

    # 2. 撤销手写条目 -> 403，文件逐字节不变
    before = live.read_bytes()
    st, body = panel.route_post("/api/overrides", {"action": "revoke", "id": "manual-1"})
    check(st == 403 and not body["ok"], "revoke manual entry -> 403")
    check(live.read_bytes() == before, "file untouched after refused revoke")
    check(not log.events and not (hist / "audit.jsonl").exists(), "refused revoke writes no audit / event")

    # 3. 不存在 -> 404
    st, _ = panel.route_post("/api/overrides", {"action": "revoke", "id": "rem-nope"})
    check(st == 404, "revoke unknown id -> 404")

    # 4. 撤销成功：另外两条和 rules/tools 不变
    st, body = panel.route_post("/api/overrides", {"action": "revoke", "id": "rem-aaaaaaaaaaaa"})
    check(st == 200 and body["ok"], "revoke remembered -> 200")
    expected = dict(levels, overrides=[manual, rem_b])
    check(live.read_text(encoding="utf-8") == format_levels(expected), "file == original minus that one entry, byte for byte")
    st, body = panel.route_get("/api/overrides", {})
    check([i["id"] for i in body["items"]] == ["rem-bbbbbbbbbbbb"], "list shrinks after revoke")

    # 5. 审计 + 事件
    lines = [json.loads(x) for x in (hist / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    check(len(lines) == 1 and lines[0]["file"] == "guanjia/levels.json" and lines[0]["actor"] == "panel"
          and lines[0]["id"] == "rem-aaaaaaaaaaaa" and lines[0]["summary"] == "exec: shutdown /s", "audit line written with id and summary")
    check(len(log.events) == 1 and log.events[0][0] == "panel" and log.events[0][3].get("id") == "rem-aaaaaaaaaaaa", "elog event emitted (comp=panel)")

    # 6. 参数错误
    for bad in ({"action": "revoke"}, {"action": "nuke", "id": "x"}):
        st, body = panel.route_post("/api/overrides", bad)  # 路由层把 ValueError 统一成 400
        check(st == 400 and not body["ok"], f"bad body {bad} -> 400")

    # 7. 文件坏了 -> 500，不写
    live.write_text('{ "broken": ', encoding="utf-8")
    check(panel.route_get("/api/overrides", {})[0] == 500, "GET unreadable -> 500")
    st, _ = panel.route_post("/api/overrides", {"action": "revoke", "id": "rem-bbbbbbbbbbbb"})
    check(st == 500 and live.read_text(encoding="utf-8") == '{ "broken": ', "revoke unreadable -> 500, file untouched")
    check(not list(tmp.glob("*.tmp")), "no temp file left behind")

    # 8. 页面里有列表与撤销入口（静态检查）
    html = PANEL_HTML.read_text(encoding="utf-8")
    check("/api/overrides" in html and "revoke" in html and "撤销后再遇到会重新问你" in html, "panel.html has remembered-list UI")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS test_p22_overrides_panel" if failed == 0 else f"{failed} FAILED test_p22_overrides_panel")
sys.exit(1 if failed else 0)
