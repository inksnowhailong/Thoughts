"""语音守护进程的本机控制口（只听 127.0.0.1:18795）。

给本机程序用（分级插件的 speak 工具、上线脚本），不带令牌：
    GET  /status                -> 托盘同款状态 JSON
    POST /say {"text": "..."}   -> 让电脑用管家音色念一句（走守护进程自己的播放队列，打断照样生效）
    POST /sleep-state {"state": "awake|sleeping|consolidating"} -> P4 休眠状态位（托盘显示）
给浏览器里的「管家面板」用（同源页面，要令牌）：
    GET  /panel                 -> 面板页面（页面里带着本次启动随机生成的令牌）
    GET  /api/snapshot、/api/utterance?id=、/api/events（SSE）   令牌放查询参数 token=
    POST /api/control、/api/settings、/api/inject                 令牌放请求头 X-Guanjia-Token
    其余 /api/* 由 PanelBackend 路由表提供（register_get / register_post，如 /api/speaker），同样要令牌与同源 Origin；
    /api/* 请求体上限 256KB，旧接口仍是 8KB。

防护：只绑 127.0.0.1、只接受本机来源；Host 必须是 127.0.0.1/localhost（挡 DNS 重绑定）；POST 必须是 application/json；
独占端口，第二个实例绑不上。旧接口带 Origin 头一律 403（浏览器网页发起的都带）；/api/* 只放行同源 Origin
（http://127.0.0.1:18795 或 http://localhost:18795），并且要令牌——别的网页读不到面板页面（同源策略），
也就拿不到令牌；跨站带自定义请求头会先触发 CORS 预检，这里不回 CORS 头，所以跨站改设置、点退出都做不到。
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

CONTROL_PORT = int(os.environ.get("GUANJIA_CONTROL_PORT", "18795"))
SAY_MAX_CHARS = 300
SLEEP_STATES = ("awake", "sleeping", "consolidating")
SSE_HEARTBEAT_SEC = 15.0
SSE_WRITE_TIMEOUT_SEC = 30.0  # 页面冻住（后台标签、休眠）不读时，写阻塞这么久就断开，让出名额
SSE_MAX_CLIENTS = 5
_MAX_BODY = 8192  # 旧接口（/say、/sleep-state）的请求体上限
_MAX_BODY_API = 262144  # /api/*（要令牌）的上限：提示词、记忆这类整份文本要能存
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class ControlAPI(Protocol):
    def say(self, text: str) -> tuple[bool, str]: ...

    def status(self) -> dict: ...

    def set_sleep_state(self, state: str) -> bool: ...


class PanelAPI(Protocol):
    token: str

    def html(self) -> bytes: ...

    def snapshot(self) -> dict: ...

    def record(self, uid: str) -> dict | None: ...

    def subscribe(self) -> queue.Queue: ...

    def unsubscribe(self, q: queue.Queue) -> None: ...

    def events_since(self, seq: int) -> list[dict]: ...

    def control(self, action: str) -> tuple[bool, str]: ...

    def update_settings(self, patch: dict) -> tuple[bool, dict | str]: ...

    def inject(self, text: str, dry_run: bool) -> dict: ...

    def route_get(self, path: str, query: dict) -> tuple[int, dict] | None: ...

    def route_post(self, path: str, body: dict) -> tuple[int, dict] | None: ...


class _ExclusiveServer(ThreadingHTTPServer):
    allow_reuse_address = False  # Windows 上 SO_REUSEADDR 会让第二个实例也绑上同一端口，宁可报错
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_control_server(api: ControlAPI, port: int = CONTROL_PORT, host: str = "127.0.0.1",
                        panel: PanelAPI | None = None) -> ThreadingHTTPServer:
    """建好但不启动；port=0 让系统挑空闲端口（测试用）。端口被占时抛 OSError。panel=None 时没有面板接口。"""
    sse_clients = [0]
    sse_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 不往 stderr 打访问日志
            pass

        def _send(self, code: int, obj: dict) -> None:
            self._drain()
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        @property
        def _port(self) -> int:
            return self.server.server_address[1]

        def _guard(self, is_api: bool) -> bool:
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._send(403, {"ok": False, "reason": "not_local"})
                return False
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in _LOCAL_HOSTS:  # DNS 重绑定：网页同源 GET 不带 Origin，但 Host 是攻击者的域名
                self._send(403, {"ok": False, "reason": "bad_host"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None:
                same = origin in (f"http://127.0.0.1:{self._port}", f"http://localhost:{self._port}")
                if not (is_api and same):
                    self._send(403, {"ok": False, "reason": "browser_origin"})
                    return False
            return True

        def _token_ok(self, query: dict) -> bool:
            if panel is None:
                self._send(404, {"ok": False, "reason": "not_found"})
                return False
            given = self.headers.get("X-Guanjia-Token") or ""
            if self.command == "GET" and not given:
                given = (query.get("token") or [""])[0]
            if not hmac.compare_digest(given.encode(), panel.token.encode()):
                self._send(403, {"ok": False, "reason": "bad_token"})
                return False
            return True

        _body_read = False

        def _body_limit(self) -> int:
            return _MAX_BODY_API if urlsplit(self.path).path.startswith("/api/") else _MAX_BODY

        def _drain(self) -> None:
            """回应前把没读的请求体读掉：Windows 上带着未读数据关连接会发 RST，客户端可能收不到这条回应。"""
            if self._body_read or self.command != "POST":
                return
            self._body_read = True
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            try:
                if 0 <= n <= self._body_limit():
                    self.rfile.read(n)
                else:
                    self.connection.settimeout(0.2)
                    self.rfile.read1(65536)
            except OSError:
                pass

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
            if n > self._body_limit():
                self._send(413, {"ok": False, "reason": "too_large"})
                return None
            self._body_read = True
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError):
                body = None
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "reason": "bad_json"})
                return None
            return body

        def do_GET(self):
            url = urlsplit(self.path)
            is_api = url.path.startswith("/api/")
            if not self._guard(is_api):
                return
            query = parse_qs(url.query)
            if url.path == "/status":
                self._send(200, api.status())
            elif url.path == "/panel" and panel is not None:
                self._send_panel()
            elif is_api and self._token_ok(query):
                if url.path == "/api/snapshot":
                    self._send(200, panel.snapshot())
                elif url.path == "/api/utterance":
                    rec = panel.record((query.get("id") or [""])[0])
                    self._send(200 if rec else 404, rec or {"ok": False, "reason": "not_found"})
                elif url.path == "/api/events":
                    self._sse()
                else:
                    r = panel.route_get(url.path, {k: v[0] for k, v in query.items()})
                    self._send(*(r if r is not None else (404, {"ok": False, "reason": "not_found"})))
            elif not is_api:
                self._send(404, {"ok": False, "reason": "not_found"})

        def do_POST(self):
            url = urlsplit(self.path)
            is_api = url.path.startswith("/api/")
            if not self._guard(is_api):
                return
            if is_api:
                if not self._token_ok({}):
                    return
            elif url.path not in ("/say", "/sleep-state"):
                self._send(404, {"ok": False, "reason": "not_found"})
                return
            body = self._json_body()
            if body is None:
                return
            if url.path == "/say":
                text = str(body.get("text") or "").strip()
                if not text:
                    self._send(400, {"ok": False, "reason": "empty"})
                    return
                if len(text) > SAY_MAX_CHARS:
                    self._send(400, {"ok": False, "reason": "too_long"})
                    return
                ok, reason = api.say(text)
                self._send(200 if ok else 409, {"ok": ok, "reason": reason})
            elif url.path == "/sleep-state":
                state = str(body.get("state") or "")
                if state not in SLEEP_STATES:
                    self._send(400, {"ok": False, "reason": "bad_state"})
                    return
                api.set_sleep_state(state)
                self._send(200, {"ok": True, "reason": "set"})
            elif url.path == "/api/control":
                ok, msg = panel.control(str(body.get("action") or ""))
                self._send(200 if ok else 400, {"ok": ok, "reason": msg})
            elif url.path == "/api/settings":
                ok, result = panel.update_settings(body)
                self._send(200 if ok else 400, {"ok": ok, "settings": result} if ok else {"ok": False, "reason": result})
            elif url.path == "/api/inject":
                text = str(body.get("text") or "").strip()
                if not text or len(text) > SAY_MAX_CHARS:
                    self._send(400, {"ok": False, "reason": "bad_text"})
                    return
                self._send(200, {"ok": True, **panel.inject(text, bool(body.get("dry_run", True)))})
            else:  # 路由表里的 /api/*；没注册的路径 404
                r = panel.route_post(url.path, body)
                self._send(*(r if r is not None else (404, {"ok": False, "reason": "not_found"})))

        def _send_panel(self) -> None:
            data = panel.html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _sse(self) -> None:
            with sse_lock:
                if sse_clients[0] >= SSE_MAX_CLIENTS:
                    self._send(429, {"ok": False, "reason": "too_many_panels"})
                    return
                sse_clients[0] += 1
            q = panel.subscribe()
            try:
                self.connection.settimeout(SSE_WRITE_TIMEOUT_SEC)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    last = int(self.headers.get("Last-Event-ID") or 0)
                except ValueError:
                    last = 0
                backlog = panel.events_since(last) if last else []
                self._write_event({"type": "hello", "seq": 0})
                for ev in backlog:
                    self._write_event(ev)
                while True:
                    try:
                        ev = q.get(timeout=SSE_HEARTBEAT_SEC)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._write_event(ev)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                panel.unsubscribe(q)
                with sse_lock:
                    sse_clients[0] -= 1

        def _write_event(self, ev: dict) -> None:
            data = json.dumps(ev, ensure_ascii=False)
            head = f"id: {ev['seq']}\n" if ev.get("seq") else ""
            self.wfile.write(f"{head}event: {ev['type']}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

    return _ExclusiveServer((host, port), Handler)


def start_control_server(api: ControlAPI, port: int = CONTROL_PORT, panel: PanelAPI | None = None) -> ThreadingHTTPServer:
    """建好并在后台线程里启动；端口被占时抛 OSError（调用方据此判断是否已有实例在跑）。"""
    server = make_control_server(api, port, panel=panel)
    threading.Thread(target=server.serve_forever, name="control", daemon=True).start()
    return server
