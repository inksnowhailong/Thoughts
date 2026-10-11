"""P2.2 B1：面板通用路由表（register_get / register_post）与 /api/* 请求体上限。
起一个只听 127.0.0.1 的临时端口，不碰音频、不碰模型。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_panel_routes.py
"""
import http.client
import json
import threading

from guanjia.control import make_control_server
from guanjia.events import EventHub
from guanjia.panel import PanelBackend
from guanjia.settings import SettingsStore


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


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


fake = FakeDaemon()
panel = PanelBackend(EventHub(), SettingsStore(None), status=fake.status, control=fake.control, inject=fake.inject,
                     token="tok-123")
seen = {}


def get_demo(query):
    seen["query"] = query
    return 200, {"ok": True, "echo": query.get("x")}


def post_demo(body):
    seen["body_len"] = len(json.dumps(body))
    if body.get("boom"):
        raise ValueError("坏参数")
    return 200, {"ok": True, "n": seen["body_len"]}


panel.register_get("/api/demo", get_demo)
panel.register_post("/api/demo", post_demo)
srv = make_control_server(fake, port=0, panel=panel)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
ORIGIN = f"http://127.0.0.1:{port}"
T = {"X-Guanjia-Token": "tok-123"}


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, dict) else body
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    return r.status, json.loads(payload or b"{}")


try:
    st, body = req("GET", "/api/demo?token=tok-123&x=hi")
    check(st == 200 and body["echo"] == "hi", "registered GET route reachable with token (query flattened)")
    check(req("GET", "/api/demo")[0] == 403, "GET route without token refused")
    check(req("GET", "/api/demo?token=tok-123", headers={"Origin": "http://evil.example"})[0] == 403, "GET route refuses foreign Origin")
    check(req("GET", "/api/demo?token=tok-123", headers={"Origin": ORIGIN})[0] == 200, "GET route allows same Origin")
    check(req("GET", "/api/nothing?token=tok-123")[0] == 404, "unregistered GET path -> 404")

    st, body = req("POST", "/api/demo", {"a": 1}, T)
    check(st == 200 and body["ok"], "registered POST route reachable with token")
    check(req("POST", "/api/demo", {"a": 1})[0] == 403, "POST route without token refused")
    check(req("POST", "/api/demo", {"a": 1}, {**T, "Origin": "http://evil.example"})[0] == 403, "POST route refuses foreign Origin")
    check(req("POST", "/api/nothing", {"a": 1}, T)[0] == 404, "unregistered POST path -> 404")
    st, body = req("POST", "/api/demo", {"boom": True}, T)
    check(st == 400 and body == {"ok": False, "reason": "坏参数"}, "ValueError in route -> 400 with reason")

    big = {"pad": "x" * 200_000}
    st, body = req("POST", "/api/demo", big, T)
    check(st == 200 and body["n"] > 200_000, "200KB JSON accepted on /api/*")
    check(req("POST", "/api/demo", {"pad": "x" * 300_000}, T)[0] == 413, "over 256KB refused on /api/*")
    check(req("POST", "/say", big)[0] == 413, "same size refused on /say (old 8KB limit)")
    check(req("POST", "/api/settings", {"pad": "x" * 20_000}, T)[0] == 400, "built-in /api/settings gets the API limit (400 not 413)")
finally:
    srv.shutdown()
    srv.server_close()

print("ALL PASS test_p22_panel_routes")
