"""P2.1 面板接口用例：页面与令牌、同源/跨站 Origin、令牌校验、控制/设置/模拟接口、SSE 事件流，以及页面静态检查。
起一个只听 127.0.0.1 的临时端口，假的守护进程回调；不碰音频，不发模型请求。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p21_panel_control.py
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from guanjia.control import make_control_server
from guanjia.events import EventHub
from guanjia.panel import PANEL_HTML, PanelBackend
from guanjia.settings import SettingsStore


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


class FakeDaemon:
    def __init__(self):
        self.said, self.actions, self.injected = [], [], []

    def say(self, text):
        self.said.append(text)
        return True, "queued"

    def status(self):
        return {"view": "listening", "label": "在听", "muted": False, "sleep": "awake"}

    def set_sleep_state(self, s):
        return True

    def control(self, action):
        self.actions.append(action)
        return True, "ok:" + action

    def inject(self, text, dry_run):
        self.injected.append((text, dry_run))
        return {"verdict": "rejected", "reason": "没有唤醒词", "text": "", "dry_run": dry_run}


fake = FakeDaemon()
hub = EventHub()
store = SettingsStore(None)
panel = PanelBackend(hub, store, status=fake.status, control=fake.control, inject=fake.inject, token="tok-123")
srv = make_control_server(fake, port=0, panel=panel)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
ORIGIN = f"http://127.0.0.1:{port}"


def req(method, path, body=None, headers=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, dict) else body
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, (payload if raw else json.loads(payload or b"{}")), hdrs


T = {"X-Guanjia-Token": "tok-123"}
try:
    # ---------- 页面 ----------
    st, html, hdrs = req("GET", "/panel", raw=True)
    page = html.decode("utf-8")
    check(st == 200 and 'const TOKEN = "tok-123";' in page and "__GUANJIA_TOKEN__" not in page, "panel page carries the token")
    check("frame-ancestors 'none'" in hdrs.get("Content-Security-Policy", "") and hdrs.get("X-Frame-Options") == "DENY",
          "panel page cannot be framed")
    check(req("GET", "/panel", headers={"Host": "evil.example:1"}, raw=True)[0] == 403, "panel refuses foreign Host (DNS rebinding)")

    # ---------- 读接口 ----------
    check(req("GET", "/api/snapshot")[0] == 403, "snapshot without token refused")
    check(req("GET", "/api/snapshot?token=bad")[0] == 403, "snapshot with wrong token refused")
    st, snap, _ = req("GET", "/api/snapshot?token=tok-123")
    check(st == 200 and snap["status"]["view"] == "listening" and snap["stages"][0] == "mic"
          and snap["settings"]["wake_words"] == ["管家"] and "health" in snap["hidden_modules"], "snapshot with token")
    check(req("GET", "/api/snapshot?token=tok-123", headers={"Origin": "http://evil.example"})[0] == 403,
          "foreign Origin refused on api")
    check(req("GET", "/api/snapshot?token=tok-123", headers={"Origin": ORIGIN})[0] == 200, "same Origin allowed on api")
    uid = hub.new_utterance("mic", text="管家开灯")
    check(req("GET", f"/api/utterance?id={uid}&token=tok-123")[1]["text"] == "管家开灯", "one utterance by id")

    # ---------- 写接口 ----------
    check(req("POST", "/api/control", {"action": "mute"})[0] == 403, "control without token refused")
    check(req("POST", "/api/control?token=tok-123", {"action": "mute"})[0] == 403, "POST does not accept token in query")
    check(req("POST", "/api/control", {"action": "mute"}, {**T, "Origin": "http://evil.example"})[0] == 403,
          "control from foreign Origin refused even with token")
    st, body, _ = req("POST", "/api/control", {"action": "mute"}, {**T, "Origin": ORIGIN})
    check(st == 200 and body["reason"] == "ok:mute" and fake.actions == ["mute"], "control from the panel page")
    check(req("POST", "/api/control", {"action": "format-c"}, T)[0] == 400, "unknown action refused")
    st, body, _ = req("POST", "/api/settings", {"wake_words": "小管", "follow_up_sec": 12}, T)
    check(st == 200 and body["settings"]["wake_words"] == ["小管"] and store.current.follow_up_sec == 12.0, "settings updated")
    st, body, _ = req("POST", "/api/settings", {"spk_threshold": 5}, T)
    check(st == 400 and "spk_threshold" in body["reason"] and store.current.spk_threshold == 0.45, "bad settings refused, unchanged")
    st, body, _ = req("POST", "/api/inject", {"text": "打开记事本"}, T)
    check(st == 200 and body["verdict"] == "rejected" and fake.injected == [("打开记事本", True)], "inject defaults to dry run")
    check(req("POST", "/api/inject", {"text": ""}, T)[0] == 400, "empty inject refused")

    # ---------- 旧接口不变 ----------
    check(req("POST", "/say", {"text": "你好"})[0] == 200 and fake.said == ["你好"], "/say still works without token")
    check(req("POST", "/say", {"text": "你好"}, {"Origin": ORIGIN})[0] == 403, "/say still refuses any Origin")
    check(req("GET", "/status")[1]["view"] == "listening", "/status unchanged")

    # ---------- SSE ----------
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/events?token=tok-123", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    check(r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream"), "event stream opens")
    first = b""
    while b"\n\n" not in first:
        first += r.read1(1024) if hasattr(r, "read1") else r.read(1)
    check(b"event: hello" in first, "stream starts with hello")
    time.sleep(0.2)
    hub.stage(uid, "wake", "blocked", "没有唤醒词")
    got = b""
    deadline = time.time() + 3
    while "没有唤醒词".encode("utf-8") not in got and time.time() < deadline:
        got += r.read1(4096) if hasattr(r, "read1") else r.read(1)
    check("没有唤醒词".encode("utf-8") in got and b"event: stage" in got, "stage event pushed live")
    c.close()
finally:
    srv.shutdown()
    srv.server_close()

# ---------- 页面静态检查 ----------
html = PANEL_HTML.read_text(encoding="utf-8")
check("__GUANJIA_TOKEN__" in html, "placeholder present in the template")
check(not re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", html) and "@import" not in html, "no external scripts, styles or CDN")
check(".innerHTML" not in html, "page never uses innerHTML (recognized text and logs rendered as text)")
for id_ in ("flow", "rows", "detail", "logs", "btnMute", "btnRestart", "btnQuit", "btnSave", "btnInject", "s_wake", "s_spk"):
    check(f'id="{id_}"' in html, f"element #{id_} present")
node = os.environ.get("GUANJIA_NODE") or shutil.which("node") or \
    r"C:\Users\inksn\AppData\Roaming\fnm\node-versions\v24.21.0\installation\node.exe"
if Path(node).exists() or shutil.which(node):
    # P4-6b 后页面有三段脚本：主脚本 + 记忆页 + gen1 暂存审阅（服务时内联），逐段检查语法
    memory_js = (Path(__file__).resolve().parents[1] / "src" / "guanjia" / "panel_memory.js").read_text(encoding="utf-8")
    gen1_js = (Path(__file__).resolve().parents[1] / "src" / "guanjia" / "panel_gen1.js").read_text(encoding="utf-8")
    scripts = [s.replace("/*__PANEL_MEMORY_JS__*/", memory_js).replace("/*__PANEL_GEN1_JS__*/", gen1_js)
               for s in re.findall(r"<script>(.*?)</script>", html, re.S)]
    check(len(scripts) == 3 and "memLoad" in scripts[1] and "gen1Refresh" in scripts[2],
          "page has the main, memory and gen1 staging scripts (inlined)")
    for n, script in enumerate(scripts):
        js = Path(tempfile.mkdtemp(prefix="gj_p21p_")) / f"panel{n}.js"
        js.write_text(script, encoding="utf-8")
        rc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        check(rc.returncode == 0, f"panel script {n} passes node --check " + rc.stderr.strip()[:200])
else:
    print("SKIP node --check (node not found)")

print("ALL PASS test_p21_panel_control")
