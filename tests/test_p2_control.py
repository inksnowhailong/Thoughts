"""P2 本机控制口用例：/say /status /sleep-state 与防护（假 API，端口由系统挑，不碰音频）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p2_control.py
"""
import threading

import httpx

from guanjia.control import CONTROL_PORT, SAY_MAX_CHARS, make_control_server


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


class FakeAPI:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.refuse = ""
        self.sleep = "awake"

    def say(self, text: str) -> tuple[bool, str]:
        if self.refuse:
            return False, self.refuse
        self.said.append(text)
        return True, "queued"

    def status(self) -> dict:
        return {"view": "listening", "sleep": self.sleep}

    def set_sleep_state(self, state: str) -> bool:
        self.sleep = state
        return True


check(CONTROL_PORT == 18795, "default control port 18795")
api = FakeAPI()
srv = make_control_server(api, port=0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
base = f"http://127.0.0.1:{port}"
c = httpx.Client(trust_env=False, timeout=5)
try:
    r = c.post(f"{base}/say", json={"text": " 管家上线了 "})
    check(r.status_code == 200 and r.json() == {"ok": True, "reason": "queued"}, "say queued")
    check(api.said == ["管家上线了"], "text trimmed before handing to daemon")
    api.refuse = "muted"
    r = c.post(f"{base}/say", json={"text": "x"})
    check(r.status_code == 409 and r.json()["reason"] == "muted", "muted -> 409 muted")
    api.refuse = ""
    r = c.post(f"{base}/say", json={"text": "   "})
    check(r.status_code == 400 and r.json()["reason"] == "empty", "empty -> 400 empty")
    r = c.post(f"{base}/say", json={"text": "字" * (SAY_MAX_CHARS + 1)})
    check(r.status_code == 400 and r.json()["reason"] == "too_long", "too long -> 400 too_long")
    r = c.post(f"{base}/say", content=b"text=hi", headers={"Content-Type": "application/x-www-form-urlencoded"})
    check(r.status_code == 415, "non-JSON -> 415")
    r = c.post(f"{base}/say", json={"text": "hi"}, headers={"Origin": "http://evil.example"})
    check(r.status_code == 403 and r.json()["reason"] == "browser_origin", "Origin header (web page) -> 403")
    r = c.post(f"{base}/say", content=b"{bad", headers={"Content-Type": "application/json"})
    check(r.status_code == 400 and r.json()["reason"] == "bad_json", "bad JSON -> 400")
    check(c.get(f"{base}/status").json() == {"view": "listening", "sleep": "awake"}, "status returns daemon status")
    r = c.post(f"{base}/sleep-state", json={"state": "sleeping"})
    check(r.status_code == 200 and api.sleep == "sleeping", "sleep state set")
    check(c.post(f"{base}/sleep-state", json={"state": "dead"}).status_code == 400, "invalid sleep state -> 400")
    check(c.get(f"{base}/nope").status_code == 404, "unknown path -> 404")
    try:
        make_control_server(api, port=port)
        dup = False
    except OSError:
        dup = True
    check(dup, "second instance cannot bind the same port")
finally:
    c.close()
    srv.shutdown()
    srv.server_close()
print("ALL PASS test_p2_control")
