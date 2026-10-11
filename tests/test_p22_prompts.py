"""P2.2 B2：提示词登记表、模板渲染、语音标记、AGENTS.md 管家规则段、SKILL.md 校验、参数表、面板路由。
全部用临时目录里的假工作区 / 假仓库 / 假覆盖目录，不碰线上工作区，也不碰 data/prompts。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_prompts.py
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia import brain, params, prompts  # noqa: E402
from guanjia.events import EventHub  # noqa: E402
from guanjia.panel import PanelBackend  # noqa: E402
from guanjia.settings import Settings, SettingsStore, from_dict  # noqa: E402
from guanjia.wsfiles import WorkspaceFiles  # noqa: E402
from guanjia.wslock import ws_lock  # noqa: E402

failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.msgs: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.msgs.append(record.getMessage())


tmp = Path(tempfile.mkdtemp(prefix="gj-prompts-"))
real_override = prompts.OVERRIDE_DIR
prompts.OVERRIDE_DIR = tmp / "overrides"  # 绝不写真正的 data/prompts
cap = Capture()
logging.getLogger("guanjia.prompts").addHandler(cap)
try:
    check(prompts.OVERRIDE_DIR != real_override, "overrides redirected to temp dir")

    # 1. 渲染与占位符
    out, unknown = prompts.render("[语音 {{DATE}}]", {"DATE": "2026-10-11"})
    check(out == "[语音 2026-10-11]" and unknown == [], "render replaces known placeholders")
    out, unknown = prompts.render("a {{FOO}} b {{date}}", {"DATE": "x"})
    check(unknown == ["FOO", "date"] and out == "a {{FOO}} b {{date}}", "unknown placeholders listed and left in place")

    # 2. 语音标记：默认渲染与旧格式逐字节相同
    check(prompts.render_voice_tag(datetime(2026, 10, 11, 18, 5)) == "[语音 2026-10-11 18:05 周日]", "default voice tag sample")
    same = True
    t = datetime(2026, 1, 1, 0, 0)
    for i in range(400):  # 覆盖每一天的周几和各种时刻
        n = t + timedelta(days=i * 1.37, minutes=i * 7)
        legacy = f"[语音 {n:%Y-%m-%d %H:%M} 周{'一二三四五六日'[n.weekday()]}]"  # 改造前 brain.voice_tag 的原样写法
        if prompts.render_voice_tag(n).encode("utf-8") != legacy.encode("utf-8") or brain.voice_tag(n).encode("utf-8") != legacy.encode("utf-8"):
            same = False
    check(same, "default template byte-identical to the legacy format (400 samples)")
    check(brain.tag_voice_text("你好", datetime(2026, 10, 11, 18, 5)) == "[语音 2026-10-11 18:05 周日] 你好", "tag_voice_text unchanged")
    check((prompts.DEFAULT_DIR / "voice_tag.txt").read_bytes() == "[语音 {{DATE}} {{TIME}} 周{{WEEKDAY}}]".encode("utf-8"),
          "default template file: single line, no trailing newline, no BOM")

    # 3. 模板保存校验
    bad_templates = {
        "unknown placeholder": "[语音 {{FOO}}]",
        "lowercase placeholder": "[语音 {{date}}]",
        "missing prefix": "{{DATE}} {{TIME}}",
        "no brackets": "语音 {{DATE}}",
        "newline inside": "[语音 {{DATE}}\n{{TIME}}]",
        "over 40 chars": "[语音 " + "字" * 41 + "]",
        "empty body": "[语音 ]",
        "inner bracket": "[语音 a]b]",
        "unbalanced braces": "[语音 {{DATE} {{TIME}}]",
    }
    for name, text in bad_templates.items():
        try:
            prompts.save_template("voice_tag", text)
            check(False, f"save_template rejects: {name}")
        except ValueError:
            check(True, f"save_template rejects: {name}")
    check(not (prompts.OVERRIDE_DIR / "voice_tag.txt").exists(), "rejected templates leave no override file")
    prompts.save_template("voice_tag", "[语音 {{TIME}} 周{{WEEKDAY}}]\n")
    check(prompts.render_voice_tag(datetime(2026, 10, 11, 18, 5)) == "[语音 18:05 周日]", "valid override takes effect")
    check(prompts.load_template("voice_tag") == ("[语音 {{TIME}} 周{{WEEKDAY}}]", True), "load_template reports override")
    prompts.reset_template("voice_tag")
    check(prompts.load_template("voice_tag")[1] is False, "reset removes override")

    # 4. 覆盖文件被手工改坏：回退内置格式并记 warning
    (prompts.OVERRIDE_DIR).mkdir(parents=True, exist_ok=True)
    for name, raw in {"no prefix": "hello".encode(), "newline": "[语音 a\nb]".encode(), "unknown": "[语音 {{X}}]".encode(),
                      "invalid utf8": b"[\xff\xfe\x80]", "empty": b""}.items():
        (prompts.OVERRIDE_DIR / "voice_tag.txt").write_bytes(raw)
        cap.msgs.clear()
        got = prompts.render_voice_tag(datetime(2026, 10, 11, 18, 5))
        check(got == "[语音 2026-10-11 18:05 周日]" and len(cap.msgs) == 1, f"broken override falls back + warns: {name}")
    prompts.reset_template("voice_tag")

    # 5. block 切分
    BEGIN, END = prompts.BLOCK_BEGIN, prompts.BLOCK_END
    pre_t = "# AGENTS\n\n开头 intro\n\n"
    tail_t = "\n\n尾巴 tail\n- keep me\n"
    agents_lf = pre_t + BEGIN + "\n规则旧版\n第二行\n" + END + tail_t
    a, b, c = prompts.split_block(agents_lf)
    check(a.endswith(BEGIN) and b == "\n规则旧版\n第二行\n" and c.startswith(END), "split_block pieces")
    for name, text in {"no markers": "plain", "dup begin": BEGIN + BEGIN + END, "reversed": END + BEGIN,
                       "no end": BEGIN + "x"}.items():
        try:
            prompts.split_block(text)
            check(False, f"split_block rejects: {name}")
        except ValueError:
            check(True, f"split_block rejects: {name}")

    # 6. 假仓库 + 假工作区 + 服务
    repo = tmp / "repo"
    (repo / "openclaw" / "workspace" / "skills" / "guanjia-notes").mkdir(parents=True)
    (repo / "openclaw" / "plugins" / "guanjia-guard" / "src").mkdir(parents=True)
    (repo / "openclaw" / "AGENTS-S2-section.md").write_text(BEGIN + "\n仓库默认规则\n" + END + "\n", encoding="utf-8", newline="")
    skill_default = "---\nname: guanjia-notes\ndescription: 记事\n---\n\n# 默认技能\n"
    (repo / "openclaw" / "workspace" / "skills" / "guanjia-notes" / "SKILL.md").write_text(skill_default, encoding="utf-8", newline="")
    (repo / "openclaw" / "plugins" / "guanjia-guard" / "src" / "guard.ts").write_text("// 确认提示 guard\n" * 500, encoding="utf-8")

    ws, hist = tmp / "ws", tmp / "hist"
    (ws / "guanjia").mkdir(parents=True)
    (ws / "skills" / "guanjia-notes").mkdir(parents=True)
    fs = WorkspaceFiles(ws, hist)
    svc = prompts.PromptService(fs, repo, now=lambda: datetime(2026, 10, 11, 18, 5), lock_timeout=0.4)

    def agents_bytes(crlf: bool) -> bytes:
        return (agents_lf.replace("\n", "\r\n") if crlf else agents_lf).encode("utf-8")

    def get(pid, **q):
        return svc.get({"id": pid, **q})

    # 6a. 列表
    (ws / "AGENTS.md").write_bytes(agents_bytes(False))
    (ws / "SOUL.md").write_text("# soul\n", encoding="utf-8")
    (ws / "skills" / "guanjia-notes" / "SKILL.md").write_text(skill_default, encoding="utf-8", newline="")
    code, lst = svc.list_all({})
    items = {i["id"]: i for i in lst["items"]}
    check(code == 200 and {"soul", "identity", "rules", "agents_rest", "voice_tag", "guard", "skill_notes", "skill_lookup",
                           "skill_launch", "ack"} <= set(items), "list has every registered prompt")
    check(items["rules"]["differs_from_default"] is True and items["skill_notes"]["differs_from_default"] is False,
          "differs_from_default: rules differ, identical skill does not")
    check(items["voice_tag"]["differs_from_default"] is False and items["voice_tag"]["effect"] == "下一轮", "template default badge")
    check(items["agents_rest"]["editable"] is False and items["guard"]["effect"] == "需重启网关" and items["ack"]["editable"] is False,
          "readonly/setting entries not editable")
    check(items["identity"]["exists"] is False and items["soul"]["budget"] == 1500, "missing file reported; budget carried")

    # 6b. block：读
    code, r = get("rules")
    check(code == 200 and r["text"] == "规则旧版\n第二行" and r["default_text"] == "仓库默认规则", "rules: text is only the block, default from repo")
    check(r["sha"] == fs.read("AGENTS.md")["sha"], "rules: sha is the whole-file sha")
    code, r = get("agents_rest")
    check("规则旧版" not in r["text"] and "keep me" in r["text"] and "开头 intro" in r["text"], "agents_rest hides the block, shows the rest")
    check(svc.post({"id": "agents_rest", "text": "x", "base_sha": r["sha"]})[0] == 400, "agents_rest is read-only")
    check(svc.post({"id": "ack", "text": "x", "base_sha": ""})[0] == 400, "setting entries are not saved here")
    check(svc.post({"id": "nope", "text": "x", "base_sha": ""})[0] == 400, "unknown id -> 400")
    code, r = get("guard")
    check(code == 200 and "guard.ts" in r["text"] and len(r["text"]) < 4200, "guard: plugin source snippet, capped")

    # 6c. block：写，段外字节不变（LF 与 CRLF 两种）
    for crlf in (False, True):
        (ws / "AGENTS.md").write_bytes(agents_bytes(crlf))
        code, r = get("rules")
        code, saved = svc.post({"id": "rules", "text": "规则新版\n第二行\n第三行", "base_sha": r["sha"]})
        eol = "\r\n" if crlf else "\n"
        want = (pre_t + BEGIN + "\n规则新版\n第二行\n第三行\n" + END + tail_t).replace("\n", eol).encode("utf-8")
        check(code == 200 and (ws / "AGENTS.md").read_bytes() == want, f"block save changes only the block ({'CRLF' if crlf else 'LF'})")
        check(saved["effect"] == "下一轮" and saved["text"] == "规则新版\n第二行\n第三行", "save returns fresh detail + effect badge")
    # 没改动：整个文件字节不变、不产生备份
    n_hist = len(fs.history("AGENTS.md"))
    code, r = get("rules")
    code, _ = svc.post({"id": "rules", "text": r["text"], "base_sha": r["sha"]})
    check(code == 200 and len(fs.history("AGENTS.md")) == n_hist, "unchanged block text writes nothing")

    # 6d. block：冲突、标记异常、标记注入、历史恢复、恢复默认
    code, r = get("rules")
    (ws / "AGENTS.md").write_bytes((ws / "AGENTS.md").read_bytes() + "\r\nmodel appended\r\n".encode("utf-8"))
    code, resp = svc.post({"id": "rules", "text": "mine", "base_sha": r["sha"]})
    check(code == 409 and "model appended" in (ws / "AGENTS.md").read_text(encoding="utf-8"), "stale sha -> 409, file untouched")
    code, r = get("rules")
    code, resp = svc.post({"id": "rules", "text": "evil " + END + " more", "base_sha": r["sha"]})
    check(code == 400, "marker injection rejected")
    code, resp = svc.post({"id": "rules", "text": "x" * 20001, "base_sha": r["sha"]})
    check(code == 400, "over hard limit rejected")
    before = (ws / "AGENTS.md").read_bytes()
    (ws / "AGENTS.md").write_bytes(before.replace(END.encode(), b"<!-- other -->"))
    code, r = get("rules")
    check(code == 400 and "标记" in r["reason"], "broken markers: GET refuses (400)")
    code, resp = svc.post({"id": "rules", "text": "x", "base_sha": fs.read("AGENTS.md")["sha"]})
    check(code == 400, "broken markers: save refuses")
    (ws / "AGENTS.md").write_bytes(before)
    code, r = get("rules")
    hv = r["history"][-1]["id"]  # 最旧的一版
    code, v = get("rules", version=hv)
    check(code == 200 and v["version_text"] == "规则旧版\n第二行", "history preview for block returns only the block of that version")
    code, resp = svc.post({"id": "rules", "action": "restore", "version": hv, "base_sha": r["sha"]})
    check(code == 200 and resp["text"] == "规则旧版\n第二行", "restore puts the old block back")
    code, r = get("rules")
    code, resp = svc.post({"id": "rules", "action": "reset", "base_sha": r["sha"]})
    check(code == 200 and resp["text"] == "仓库默认规则" and resp["differs_from_default"] is False, "reset block writes repo default")
    check("model appended" in (ws / "AGENTS.md").read_text(encoding="utf-8") and "keep me" in (ws / "AGENTS.md").read_text(encoding="utf-8"),
          "reset keeps everything outside the block")

    # 6e. SKILL.md 校验
    code, r = get("skill_notes")
    base = r["sha"]
    code, resp = svc.post({"id": "skill_notes", "text": skill_default.replace("name: guanjia-notes", "name: hacked"), "base_sha": base})
    check(code == 400 and "name" in resp["reason"], "SKILL.md name change rejected")
    code, resp = svc.post({"id": "skill_notes", "text": "# no frontmatter\n", "base_sha": base})
    check(code == 400, "SKILL.md without frontmatter rejected")
    code, resp = svc.post({"id": "skill_notes", "text": skill_default.replace("---\n\n#", "\n\n#"), "base_sha": base})
    check(code == 400, "SKILL.md with unclosed frontmatter rejected")
    code, resp = svc.post({"id": "skill_notes", "text": skill_default + "\n加一句\n", "base_sha": base})
    check(code == 200 and resp["differs_from_default"] is True and "加一句" in resp["text"], "SKILL.md normal edit saved")
    code, resp = svc.post({"id": "skill_notes", "action": "reset", "base_sha": resp["sha"]})
    check(code == 200 and resp["differs_from_default"] is False, "SKILL.md reset to repo default")

    # 6f. 整文件：新建、没有默认值的 reset、冲突
    code, r = get("identity")
    check(code == 200 and r["exists"] is False and r["sha"] == "", "missing workspace file reads as empty")
    code, resp = svc.post({"id": "identity", "text": "# 身份\n", "base_sha": ""})
    check(code == 200 and (ws / "IDENTITY.md").read_text(encoding="utf-8") == "# 身份\n", "create IDENTITY.md with empty base_sha")
    code, resp = svc.post({"id": "identity", "action": "reset", "base_sha": resp["sha"]})
    check(code == 400, "reset without repo default -> 400")
    code, resp = svc.post({"id": "identity", "text": "x", "base_sha": "stale"})
    check(code == 409, "workspace file stale sha -> 409")

    # 6g. 锁被占：423
    code, r = get("soul")
    with ws_lock(path=fs.lock_path, holder="sleep"):
        code, resp = svc.post({"id": "soul", "text": "# soul2\n", "base_sha": r["sha"]})
        code2, resp2 = svc.post({"id": "rules", "text": "x", "base_sha": get("rules")[1]["sha"]})
    check(code == 423 and "sleep" in resp["reason"] and code2 == 423, "lock held elsewhere -> 423 naming the holder")
    check((ws / "SOUL.md").read_text(encoding="utf-8") == "# soul\n", "423 leaves the file untouched")

    # 6h. 模板：读、预览、保存、恢复默认
    code, r = get("voice_tag")
    check(code == 200 and r["preview"] == "[语音 2026-10-11 18:05 周日]" and r["valid"] and r["overridden"] is False, "template get: preview + default")
    code, p = svc.post({"id": "voice_tag", "action": "preview", "text": "[语音 {{FOO}}]"})
    check(code == 200 and p["valid"] is False and p["unknown"] == ["FOO"], "template preview flags unknown placeholder")
    code, resp = svc.post({"id": "voice_tag", "text": "[语音 {{FOO}}]", "base_sha": ""})
    check(code == 400 and not (prompts.OVERRIDE_DIR / "voice_tag.txt").exists(), "template save rejects bad template")
    code, resp = svc.post({"id": "voice_tag", "text": "[语音 {{DATE}} 周{{WEEKDAY}}]", "base_sha": ""})
    check(code == 200 and resp["overridden"] is True and resp["differs_from_default"] is True, "template save ok -> overridden")
    code, resp = svc.post({"id": "voice_tag", "action": "reset", "base_sha": ""})
    check(code == 200 and resp["overridden"] is False and not (prompts.OVERRIDE_DIR / "voice_tag.txt").exists(), "template reset")

    # 7. 参数表
    cur = from_dict({"spk_threshold": 0.6})
    rows = {r["key"]: r for r in params.describe(cur, {"MIC_GAIN": (3.0, 3.0), "BARGE_MS": (200, 180)})}
    need = {"key", "label", "node", "unit", "default", "current", "overridden", "effect", "editable", "source"}
    check(all(need <= set(r) for r in rows.values()), "every param row has the full shape")
    check(rows["spk_threshold"]["overridden"] is True and rows["spk_threshold"]["current"] == 0.6
          and rows["spk_threshold"]["default"] == Settings().spk_threshold, "overridden flag set for non-default setting")
    check(rows["ack_text"]["overridden"] is False and rows["ack_text"]["effect"] == "立即" and rows["ack_text"]["editable"], "default setting not overridden")
    check({p[0] for p in params.SETTING_PARAMS} == set(Settings.__dataclass_fields__), "SETTING_PARAMS covers every Settings field")
    check(rows["BARGE_MS"]["overridden"] and rows["MIC_GAIN"]["editable"] is False and rows["MIC_GAIN"]["effect"] == "需重启语音", "consts: readonly + restart badge")
    check(set(rows) == {p[0] for p in params.SETTING_PARAMS} | {"MIC_GAIN", "BARGE_MS"}, "consts not provided are omitted")

    # 8. 面板路由
    panel = PanelBackend(EventHub(), SettingsStore(None), status=lambda: {}, control=lambda a: (True, a), inject=lambda t, d: {},
                         token="t")
    panel.attach_prompts(svc, lambda: {"MIC_GAIN": (3.0, 3.0)})
    code, d = panel.route_get("/api/params", {})
    check(code == 200 and any(r["key"] == "MIC_GAIN" for r in d["items"]) and any(r["key"] == "follow_up_sec" for r in d["items"]), "GET /api/params")
    code, d = panel.route_get("/api/prompts", {})
    check(code == 200 and len(d["items"]) == len(prompts.PROMPTS), "GET /api/prompts")
    code, d = panel.route_get("/api/prompt", {"id": "soul"})
    check(code == 200 and d["text"] == "# soul\n", "GET /api/prompt")
    code, d = panel.route_post("/api/prompt", {"id": "soul", "text": "# soul 2\n", "base_sha": d["sha"]})
    check(code == 200 and (ws / "SOUL.md").read_text(encoding="utf-8") == "# soul 2\n", "POST /api/prompt")
    check(panel.route_get("/api/prompt", {"id": "zzz"})[0] == 400, "unknown id via router -> 400")

    # 9. 经真实控制口（带令牌）走一遍；页面里有抽屉/编辑器、没有 innerHTML
    import http.client
    import json
    import threading

    from guanjia.control import make_control_server

    class FakeDaemon:
        def say(self, text):
            return True, "queued"

        def status(self):
            return {}

        def set_sleep_state(self, s):
            return True

        def control(self, action):
            return True, action

        def inject(self, text, dry_run):
            return {}

    srv = make_control_server(FakeDaemon(), port=0, panel=panel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(method, path, body=None, token=True):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": f"127.0.0.1:{port}"}
        if token:
            h["X-Guanjia-Token"] = "t"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if data is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    s, raw = call("GET", "/api/prompt?id=soul&token=t", token=False)
    check(s == 200 and json.loads(raw)["text"] == "# soul 2\n", "HTTP GET /api/prompt with token")
    s, _ = call("GET", "/api/prompt?id=soul", token=False)
    check(s == 403, "HTTP GET /api/prompt without token -> 403")
    sha_now = json.loads(raw)["sha"]
    s, raw = call("POST", "/api/prompt", {"id": "soul", "text": "字" * 15000, "base_sha": sha_now})
    check(s == 200 and len(json.loads(raw)["text"]) == 15000, "HTTP POST big prompt (45KB) accepted")
    s, _ = call("POST", "/api/prompt", {"id": "soul", "text": "x", "base_sha": sha_now})
    check(s == 409, "HTTP POST stale sha -> 409")
    s, _ = call("POST", "/api/prompt", {"id": "soul", "text": "x", "base_sha": "q"}, token=False)
    check(s == 403, "HTTP POST without token -> 403")
    srv.shutdown()
    html = (ROOT / "src" / "guanjia" / "panel.html").read_text(encoding="utf-8")
    check('id="drawer"' in html and 'id="edText"' in html and "innerHTML" not in html, "panel.html has drawer + editor, no innerHTML")

finally:
    logging.getLogger("guanjia.prompts").removeHandler(cap)
    prompts.OVERRIDE_DIR = real_override
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS test_p22_prompts" if failed == 0 else f"{failed} FAILED test_p22_prompts")
sys.exit(1 if failed else 0)
