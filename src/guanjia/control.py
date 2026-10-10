"""语音守护进程的本机控制口（只听 127.0.0.1）：

    GET  /status                -> 托盘同款状态 JSON
    POST /say {"text": "..."}   -> 让电脑用管家音色念一句（走守护进程自己的播放队列，打断照样生效）
    POST /sleep-state {"state": "awake|sleeping|consolidating"} -> P4 休眠状态位（托盘显示）

防护：只绑 127.0.0.1、只接受本机来源；Host 必须是 127.0.0.1/localhost（挡 DNS 重绑定）；带 Origin 头（浏览器网页发起）一律 403；POST 必须是 application/json
（浏览器跨站发 JSON 要先过 CORS 预检，这里不回 CORS 头，网页就发不进来）；独占端口，第二个实例绑不上。
"""

from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

CONTROL_PORT = int(os.environ.get("GUANJIA_CONTROL_PORT", "18795"))
SAY_MAX_CHARS = 300
SLEEP_STATES = ("awake", "sleeping", "consolidating")
_MAX_BODY = 8192
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class ControlAPI(Protocol):
    def say(self, text: str) -> tuple[bool, str]: ...

    def status(self) -> dict: ...

    def set_sleep_state(self, state: str) -> bool: ...


class _ExclusiveServer(ThreadingHTTPServer):
    allow_reuse_address = False  # Windows 上 SO_REUSEADDR 会让第二个实例也绑上同一端口，宁可报错
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_control_server(api: ControlAPI, port: int = CONTROL_PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """建好但不启动；port=0 让系统挑空闲端口（测试用）。端口被占时抛 OSError。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 不往 stderr 打访问日志
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> bool:
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._send(403, {"ok": False, "reason": "not_local"})
                return False
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in _LOCAL_HOSTS:  # DNS 重绑定：网页同源 GET 不带 Origin，但 Host 是攻击者的域名
                self._send(403, {"ok": False, "reason": "bad_host"})
                return False
            if self.headers.get("Origin"):
                self._send(403, {"ok": False, "reason": "browser_origin"})
                return False
            return True

        def _json_body(self) -> dict | None:
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                self._send(415, {"ok": False, "reason": "need_json"})
                return None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:
                self._send(400, {"ok": False, "reason": "bad_length"})
                return None
            if n > _MAX_BODY:
                self._send(413, {"ok": False, "reason": "too_large"})
                return None
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError):
                body = None
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "reason": "bad_json"})
                return None
            return body

        def do_GET(self):
            if not self._guard():
                return
            if self.path == "/status":
                self._send(200, api.status())
            else:
                self._send(404, {"ok": False, "reason": "not_found"})

        def do_POST(self):
            if not self._guard():
                return
            if self.path not in ("/say", "/sleep-state"):
                self._send(404, {"ok": False, "reason": "not_found"})
                return
            body = self._json_body()
            if body is None:
                return
            if self.path == "/say":
                text = str(body.get("text") or "").strip()
                if not text:
                    self._send(400, {"ok": False, "reason": "empty"})
                    return
                if len(text) > SAY_MAX_CHARS:
                    self._send(400, {"ok": False, "reason": "too_long"})
                    return
                ok, reason = api.say(text)
                self._send(200 if ok else 409, {"ok": ok, "reason": reason})
                return
            state = str(body.get("state") or "")
            if state not in SLEEP_STATES:
                self._send(400, {"ok": False, "reason": "bad_state"})
                return
            api.set_sleep_state(state)
            self._send(200, {"ok": True, "reason": "set"})

    return _ExclusiveServer((host, port), Handler)


def start_control_server(api: ControlAPI, port: int = CONTROL_PORT) -> ThreadingHTTPServer:
    """建好并在后台线程里启动；端口被占时抛 OSError（调用方据此判断是否已有实例在跑）。"""
    server = make_control_server(api, port)
    threading.Thread(target=server.serve_forever, name="control", daemon=True).start()
    return server
