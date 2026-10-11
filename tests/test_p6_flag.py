"""B6：面板「这句有问题」——/api/flag 写 user_flag 事件并在记录上打标记；面板改设置 / 控制进事件流；logq --flags 能列出来；页面带按钮。
起一个只听 127.0.0.1 的临时端口，不碰音频、不碰模型。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p6_flag.py
"""
import contextlib
import http.client
import io
import json
import tempfile
import threading
from pathlib import Path

from guanjia import elog, logq
from guanjia.control import make_control_server
from guanjia.events import EventHub
from guanjia.panel import PANEL_HTML, PanelBackend
from guanjia.settings import SettingsStore


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def events(dirpath, *evs):
    out = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
    return [e for e in out if not evs or e["ev"] in evs]


class FakeDaemon:
    def say(self, text):
        return True, "queued"

    def status(self):
        return {"view": "listening"}

    def set_sleep_state(self, s):
        return True

    def control(self, action):
        return True, "ok:" + action

    def inject(self, text, dry_run):
        return {"verdict": "rejected", "reason": "x", "text": "", "dry_run": dry_run}


with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    log = elog.EventLog("voice", d, sync=True)
    try:
        hub = EventHub(elog=log)
        fake = FakeDaemon()
        panel = PanelBackend(hub, SettingsStore(None), status=fake.status, control=fake.control, inject=fake.inject, token="tok-1")
        # 没接事件日志之前：控制 / 改设置照常工作，只是不记
        check(panel.control("mute") == (True, "ok:mute") and not events(d), "without attach_flags control works and nothing is logged")
        panel.attach_flags(log)

        uid = hub.new_utterance("mic", text="打开音响", reply="")
        hub.finish(uid, verdict="accept", reply="我先查一下")
        st, body = panel.route_post("/api/flag", {"uid": uid, "note": "没打开"})
        check(st == 200 and body == {"ok": True}, "flag route returns 200")
        fl = events(d, "user_flag")
        check(len(fl) == 1 and fl[0]["uid"] == uid and fl[0]["level"] == "warn" and fl[0]["msg"] == "没打开", "user_flag event: uid, warn, note as msg")
        check(fl[0]["data"] == {"text": "打开音响", "reply": "我先查一下", "verdict": "accept"}, "text / reply / verdict come from the hub record")
        check(hub.record(uid)["flag"] == "没打开", "the panel record carries the flag")
        flag_records = [e for e in hub.events_since(0) if e["type"] == "record" and e["record"].get("flag") == "没打开"]
        check(len(flag_records) == 1, "panel gets a record update so the badge shows without refresh")

        st, _ = panel.route_post("/api/flag", {"uid": uid})
        check(st == 200 and events(d, "user_flag")[-1]["msg"] == "（没写备注）" and hub.record(uid)["flag"] == "有问题", "empty note is allowed")
        check(panel.route_post("/api/flag", {"note": "x"})[0] == 400 and panel.route_post("/api/flag", {"uid": "  "})[0] == 400, "missing uid -> 400")
        st, _ = panel.route_post("/api/flag", {"uid": "u000000-999", "note": "老的"})
        last = events(d, "user_flag")[-1]
        check(st == 200 and last["uid"] == "u000000-999" and "text" not in last.get("data", {}), "uid no longer in the hub: still 200, text empty")
        panel.route_post("/api/flag", {"uid": uid, "note": "x" * 500})
        check(len(events(d, "user_flag")[-1]["msg"]) <= 200, "note is capped")

        # 面板控制 / 改设置
        panel.control("restart")
        check(events(d, "panel.control")[-1]["data"] == {"action": "restart"}, "panel.control recorded")
        check(panel.control("bogus")[0] is False and len(events(d, "panel.control")) == 1, "unknown action neither runs nor logs")
        ok, new = panel.update_settings({"spk_threshold": 0.33})
        ev = events(d, "panel.settings")[-1]
        check(ok and ev["data"]["changed"] == ["spk_threshold"] and ev["data"]["patch"] == {"spk_threshold": 0.33}, "panel.settings: which keys, which values")
        ok, err = panel.update_settings({"spk_threshold": "abc"})
        check(not ok and len(events(d, "panel.settings")) == 1, "rejected settings are not logged as changes")

        # 令牌：/api/flag 走通用路由，需要令牌
        srv = make_control_server(fake, port=0, panel=panel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def post(headers):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/api/flag", body=json.dumps({"uid": uid, "note": "via http"}), headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **headers})
            r = c.getresponse()
            r.read()
            c.close()
            return r.status

        try:
            check(post({}) == 403 and post({"X-Guanjia-Token": "wrong"}) == 403, "/api/flag without the token is refused")
            n = len(events(d, "user_flag"))
            check(post({"X-Guanjia-Token": "tok-1"}) == 200 and len(events(d, "user_flag")) == n + 1, "/api/flag with the token works")
        finally:
            srv.shutdown()
            srv.server_close()

        # logq --flags
        out = d / "out.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            logq.main(["--flags", "--log-dir", str(d), "--out", str(out)])
        text = out.read_text(encoding="utf-8")
        check(uid in text and "没打开" in text and "「打开音响」→「我先查一下」" in text, "logq --flags lists the flagged sentence with text and reply")
    finally:
        log.close()

html = PANEL_HTML.read_text(encoding="utf-8")
check('data-alt": "flag-button"' in html and "/api/flag" in html and "这句有问题" in html, "panel.html has the flag button wired to /api/flag")
check("r.flag" in html, "panel.html shows the flag badge")

print("ALL PASS test_p6_flag")
