"""P2.2 B3：记忆页后端（USER 卡片解析、MEMORY 新建、时间线旧行警告、family-facts 只许追加、白名单、冲突、页面接线）。
全部用临时目录里的假工作区，不碰线上 C:\\Users\\inksn\\.openclaw\\workspace。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_memory.py
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
from guanjia.memory_page import MemoryService, check_facts, parse_user_cards  # noqa: E402
from guanjia.panel import PANEL_HTML, PanelBackend  # noqa: E402
from guanjia.settings import SettingsStore  # noqa: E402
from guanjia.wsfiles import WorkspaceFiles  # noqa: E402

failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


class FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, comp, ev, msg="", **kw):
        self.events.append((comp, ev, msg, kw))


FACTS = {"_说明": "改完即时生效，只许末尾追加", "people": ["妈妈", "爸爸"], "rooms": ["客厅"], "ips": []}
USER_MD = """# USER
<!-- observed: YYYY-MM-DD | status: active -->
- 示例条目（模板）
<!-- observed: 2026-10-10 | status: active -->
- 回答尽量简短
<!-- observed: 2026-10-01 | status: superseded -->
- 早上不要说天气

- 没有注释的一行不算卡片
"""

tmp = Path(tempfile.mkdtemp(prefix="gj-mem-"))
try:
    ws, hist = tmp / "workspace", tmp / "history"
    (ws / "guanjia").mkdir(parents=True)
    (ws / "memory").mkdir()
    fs = WorkspaceFiles(ws, hist)
    log = FakeLog()
    svc = MemoryService(fs, log)

    def body(rel):
        return svc.get({"rel": rel})[1]

    def save(rel, text, **kw):
        return svc.post({"rel": rel, "text": text, "base_sha": body(rel)["sha"], **kw})

    # 1. 清单：四个固定文件，全都还不存在；没有每日笔记
    st, d = svc.list_all()
    check(st == 200 and [i["rel"] for i in d["items"]] == ["USER.md", "MEMORY.md", "memory/timeline.md", "guanjia/family-facts.json"], "list: four fixed files")
    check(all(not i["exists"] for i in d["items"]) and d["daily"] == [], "list: nothing exists yet, no daily notes")
    check({i["rel"]: i["budget"] for i in d["items"]}["USER.md"] == 3600 and {i["rel"]: i["budget"] for i in d["items"]}["MEMORY.md"] == 6000, "list: soft budgets USER 3600 / MEMORY 6000")

    # 2. USER.md 卡片解析
    cards = parse_user_cards(USER_MD)
    check(len(cards) == 3 and [c["status"] for c in cards] == ["active", "active", "superseded"], "cards: statuses parsed")
    check(cards[0]["placeholder"] and not cards[1]["placeholder"], "cards: YYYY-MM-DD template entry marked as placeholder")
    check(cards[1]["text"] == "回答尽量简短" and cards[1]["date"] == "2026-10-10" and cards[2]["text"] == "早上不要说天气", "cards: date and instruction text")
    (ws / "USER.md").write_bytes(USER_MD.replace("\n", "\r\n").encode("utf-8"))
    check(len(body("USER.md")["cards"]) == 3, "get USER.md returns cards")
    check("cards" not in body("MEMORY.md"), "cards only for USER.md")

    # 3. MEMORY.md 新建（base_sha 为空）+ 冲突
    st, r = svc.post({"rel": "MEMORY.md", "text": "# M\n- 事实一\n", "base_sha": ""})
    check(st == 200 and r["exists"] and (ws / "MEMORY.md").read_text(encoding="utf-8") == "# M\n- 事实一\n", "create MEMORY.md with empty base_sha")
    sha_open = body("MEMORY.md")["sha"]
    (ws / "MEMORY.md").write_text("# M\nmodel wrote\n", encoding="utf-8")  # 模型在你打开之后改了
    st, r = svc.post({"rel": "MEMORY.md", "text": "mine\n", "base_sha": sha_open})
    check(st == 409 and "model wrote" in (ws / "MEMORY.md").read_text(encoding="utf-8"), "stale base_sha -> 409, file untouched")
    st, r = save("MEMORY.md", "# M\n- 事实一\n- 事实二\n")
    check(st == 200 and len(r["history"]) >= 1, "save with fresh sha -> 200 and old version in history")
    check(any(e[1] == "memory.save" and e[0] == "panel" and e[3]["rel"] == "MEMORY.md" for e in log.events), "elog memory.save event (comp=panel)")
    check((hist / "audit.jsonl").exists() and "MEMORY.md" in (hist / "audit.jsonl").read_text(encoding="utf-8"), "audit line written by WorkspaceFiles")

    # 4. 白名单：记忆页只管自己那几个
    for bad in ("SOUL.md", "AGENTS.md", "../x.md", "memory/notes.md", "skills/guanjia-notes/SKILL.md", ""):
        check(svc.get({"rel": bad})[0] == 400 and svc.post({"rel": bad, "text": "x", "base_sha": ""})[0] == 400, f"reject rel {bad!r} (400)")
    check(not (ws / "SOUL.md").exists(), "nothing written for rejected rel")

    # 5. 每日笔记：按日期命名，新建后出现在清单里；超长被拒
    st, r = svc.post({"rel": "memory/2026-10-11.md", "text": "今天的笔记\n", "base_sha": ""})
    check(st == 200 and svc.list_all()[1]["daily"][0]["rel"] == "memory/2026-10-11.md", "daily note created and listed")
    check(svc.post({"rel": "memory/2026-13-99x.md", "text": "x", "base_sha": ""})[0] == 400, "bad daily name rejected")
    check(svc.post({"rel": "MEMORY.md", "text": "字" * 20001, "base_sha": body("MEMORY.md")["sha"]})[0] == 400, "over 20000 chars -> 400")

    # 6. 时间线：追加没警告；改旧行保存成功但带黄色警告
    (ws / "memory" / "timeline.md").write_text("| 时间 | 事 |\n| 1 | a |\n", encoding="utf-8")
    st, r = save("memory/timeline.md", "| 时间 | 事 |\n| 1 | a |\n| 2 | b |\n")
    check(st == 200 and "warn" not in r, "timeline append: no warning")
    st, r = save("memory/timeline.md", "| 时间 | 事 |\n| 1 | CHANGED |\n| 2 | b |\n")
    check(st == 200 and "旧行" in r["warn"] and "CHANGED" in (ws / "memory" / "timeline.md").read_text(encoding="utf-8"), "timeline old line edit: allowed with yellow warning")
    check("只追加" in body("memory/timeline.md")["note"], "timeline note carries the append-only convention")

    # 7. family-facts：只许末尾追加
    facts_p = ws / "guanjia" / "family-facts.json"
    facts_text = json.dumps(FACTS, ensure_ascii=False, indent=2)
    st, r = svc.post({"rel": "guanjia/family-facts.json", "text": facts_text, "base_sha": ""})
    check(st == 200, "create family-facts.json (valid JSON object)")
    good = dict(FACTS, people=FACTS["people"] + ["奶奶"], ips=["192.168.1.2"])
    st, r = save("guanjia/family-facts.json", json.dumps(good, ensure_ascii=False, indent=2))
    check(st == 200 and json.loads(facts_p.read_text(encoding="utf-8"))["people"] == ["妈妈", "爸爸", "奶奶"], "append to arrays at the end: accepted")
    cur = json.loads(facts_p.read_text(encoding="utf-8"))
    cases = {
        "delete middle item": dict(cur, people=["妈妈", "奶奶"]),
        "reorder": dict(cur, people=["爸爸", "妈妈", "奶奶"]),
        "edit item": dict(cur, people=["妈妈", "爸爸", "奶奶"], rooms=["卧室"]),
        "delete key": {k: v for k, v in cur.items() if k != "rooms"},
        "change _说明": dict(cur, **{"_说明": "改了"}),
        "array became string": dict(cur, rooms="客厅"),
    }
    before = facts_p.read_bytes()
    for name, nd in cases.items():
        st, r = save("guanjia/family-facts.json", json.dumps(nd, ensure_ascii=False, indent=2))
        check(st == 400 and r["reason"], f"facts {name}: rejected with a reason ({r.get('reason')})")
    st, r = save("guanjia/family-facts.json", '{"people": [')
    check(st == 400 and "JSON" in r["reason"], "facts broken JSON rejected")
    st, r = save("guanjia/family-facts.json", "[1, 2]")
    check(st == 400, "facts top-level array rejected")
    check(facts_p.read_bytes() == before, "family-facts.json byte-identical after every rejection")
    st, r = save("guanjia/family-facts.json", json.dumps(dict(cur, desks=["x"]), ensure_ascii=False))
    check(st == 200 and "desks" in json.loads(facts_p.read_text(encoding="utf-8")), "adding a brand-new array key is allowed")
    check(check_facts(None, "{}") is None, "check_facts: no old file -> valid object is enough")

    # 8. 历史恢复：普通文件能恢复；family-facts 恢复成会删项的旧版会被同一规则拒绝
    h = body("MEMORY.md")["history"]
    old_id = h[-1]["id"]
    st, r = svc.post({"rel": "MEMORY.md", "action": "restore", "version": old_id, "base_sha": body("MEMORY.md")["sha"]})
    check(st == 200 and r["text"] != "", "restore a history version of MEMORY.md")
    check(svc.post({"rel": "MEMORY.md", "action": "restore", "version": "bad", "base_sha": body("MEMORY.md")["sha"]})[0] == 400, "bad version id -> 400")
    fh = body("guanjia/family-facts.json")["history"]
    st, r = svc.post({"rel": "guanjia/family-facts.json", "action": "restore", "version": fh[-1]["id"], "base_sha": body("guanjia/family-facts.json")["sha"]})
    check(st == 400, "restoring an older facts version that drops items is rejected")
    check(svc.post({"rel": "MEMORY.md", "action": "nuke", "base_sha": ""})[0] == 400, "unknown action -> 400")

    # 9. 页面接线：路由已注册；页面里有记忆区和脚本
    panel = PanelBackend(EventHub(), SettingsStore(None), status=lambda: {}, control=lambda a: (True, a), inject=lambda t, d: {},
                         token="tok", levels_path=tmp / "levels.json", audit_dir=tmp / "audit")
    panel.attach_memory(svc)
    check(panel.route_get("/api/memory", {})[0] == 200 and panel.route_get("/api/memory/file", {"rel": "USER.md"})[0] == 200
          and panel.route_post("/api/memory/file", {"rel": "MEMORY.md", "action": "nuke"})[0] == 400, "routes registered on the panel")
    page = panel.html().decode("utf-8")
    html = PANEL_HTML.read_text(encoding="utf-8")
    check('id="memSection"' in html and "__PANEL_MEMORY_JS__" in html, "panel.html has the memory section and script slot")
    check("/api/memory/file" in page and "__PANEL_MEMORY_JS__" not in page and "tok" in page, "served page has the memory script inlined (token filled)")
    check("textContent" in page and "innerHTML" not in Path(PANEL_HTML.with_name("panel_memory.js")).read_text(encoding="utf-8"), "memory script renders with textContent only")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS test_p22_memory" if failed == 0 else f"{failed} FAILED test_p22_memory")
sys.exit(1 if failed else 0)
