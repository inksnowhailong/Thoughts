"""bridge 服务：TS Agent Runtime ↔ Python 权限/工具/审计的本地 HTTP 桥。

python -m guanjia.bridge_service，只听 127.0.0.1:18801（GUANJIA_BRIDGE_PORT 可改）。

接口（全部 JSON；鉴权 Authorization: Bearer <token>）：
    POST /internal/policy/check   {session_id, capability_id, args, intent?, task_id?, credential?}
                                 → {outcome, reason, todo_id?, summary?}
    POST /internal/policy/confirm {todo_id, decision} → {credential} | {denied: true}
    POST /internal/tools/<id>/exec {args, session_id, credential?}（credential 也可放
                                 x-guanjia-credential 头）→ {ok, output|error}
    POST /internal/audit          {event} → {ok, recorded}
    POST /internal/bridge/worker_approval
                                 {task_id, capability, args_summary, todo_id, callback?, run_id?}
                                 → {ok, id}
审批兼容口（给现有 approval_ipc.py 的同形接口；额外接受 X-Guanjia-Approval-Cap 头）：
    GET  /latest[?session=]       → {ok, pending: {id, summary, status, expiresAt} | null}
    POST /decide {id, decision}   → {ok}（已决定/过期 409）
    （/internal/approvals/latest、/internal/approvals/decide 为同实现的别名。）

token：GUANJIA_BRIDGE_TOKEN 优先，缺省读 agent-runtime/config/runtime.json 的 token 字段，
再缺省每次启动随机生成并打印到 stderr。审批能力串 GUANJIA_APPROVAL_CAP 同理。
未知路由一律 404 JSON。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import httpx

from guanjia.memory.service import MemoryService
from guanjia.permission import SQLitePermissionStore
from guanjia.permission.audit import PermissionAudit, SQLitePermissionAuditStore

from guanjia.bridge import (
    ApprovalRegistry,
    BridgeAudit,
    BridgePolicy,
    CredentialLedger,
    execute_capability,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 18801
_MAX_BODY = 262144
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_token() -> str:
    """GUANJIA_BRIDGE_TOKEN → agent-runtime/config/runtime.json 的 token → 随机生成。"""
    env = os.environ.get("GUANJIA_BRIDGE_TOKEN", "").strip()
    if env:
        return env
    runtime_json = REPO_ROOT / "agent-runtime" / "config" / "runtime.json"
    try:
        parsed = json.loads(runtime_json.read_text(encoding="utf-8"))
        token = parsed.get("token") if isinstance(parsed, dict) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    except (OSError, ValueError):
        pass
    return secrets.token_urlsafe(24)


def resolve_approval_cap() -> str:
    env = os.environ.get("GUANJIA_APPROVAL_CAP", "").strip()
    if env:
        return env
    return secrets.token_urlsafe(32)


class BridgeApp:
    """策略、审批、凭证、工具与审计的接线（HTTP 层只做路由与鉴权）。"""

    def __init__(self, data_dir: Path | None = None, now_fn: Callable[[], datetime] = _utc_now,
                 tool_overrides: dict[str, Callable[[dict], dict]] | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else REPO_ROOT / "data" / "agent-runtime"
        self._now = now_fn
        self.store = SQLitePermissionStore(self.data_dir / "permission-todos.sqlite3")
        audit_store = SQLitePermissionAuditStore(self.data_dir / "permission-audit.sqlite3")
        self.permission_audit = PermissionAudit(audit_store)
        self.audit = BridgeAudit(self.permission_audit, self.data_dir / "bridge-audit.jsonl")
        self.approvals = ApprovalRegistry()
        self.ledger = CredentialLedger()
        self.policy = BridgePolicy(self.store, self.approvals, self.ledger, now_fn=now_fn)
        self.tool_overrides = dict(tool_overrides or {})
        # Phase 4：记忆检索（数据在 data/memory/memory.sqlite3，导入走 staging/commit 链）
        self.memory = MemoryService(self.data_dir.parent / "memory" / "memory.sqlite3")

    def now(self) -> datetime:
        return self._now()

    # ---------- /internal/policy/check ----------
    def policy_check(self, body: dict) -> tuple[int, dict]:
        session_id = str(body.get("session_id") or "voice-main").strip()
        capability_id = str(body.get("capability_id") or "").strip()
        if not capability_id:
            return 400, {"ok": False, "reason": "missing_capability_id"}
        if not isinstance(body.get("args"), dict):
            return 400, {"ok": False, "reason": "args_must_be_object"}
        intent = body.get("intent")
        task_id = body.get("task_id")
        credential = body.get("credential")
        decision = self.policy.check(
            session_id, capability_id, body.get("args"),
            intent=str(intent) if isinstance(intent, str) and intent.strip() else None,
            task_id=str(task_id) if isinstance(task_id, str) and task_id.strip() else None,
            credential=str(credential) if isinstance(credential, str) and credential.strip() else None,
        )
        if decision["outcome"] == "confirmation_required":
            context = self.policy.confirmation_request(decision["todo_id"])
            if context is not None:
                request, decision_obj, at = context
                self.audit.record_confirmation(request, decision_obj, decision["todo_id"], at)
        return 200, decision

    # ---------- /internal/policy/confirm ----------
    def policy_confirm(self, body: dict) -> tuple[int, dict]:
        todo_id = str(body.get("todo_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        if not todo_id or decision not in ("approve", "reject"):
            return 400, {"ok": False, "reason": "need_todo_id_and_decision"}
        try:
            result = self.policy.confirm(todo_id, decision)
        except ValueError as exc:
            reason = str(exc) or "bad_request"
            status = {"unknown": 404, "expired": 409, "already_used": 409}.get(reason, 400)
            return status, {"ok": False, "reason": reason}
        return 200, result

    # ---------- /internal/tools/<id>/exec ----------
    def tools_exec(self, capability_id: str, body: dict, header_credential: str | None) -> tuple[int, dict]:
        session_id = str(body.get("session_id") or "voice-main").strip()
        args = body.get("args")
        if not isinstance(args, dict):
            return 400, {"ok": False, "error": {"code": "bad_args", "message": "args 必须是对象"}}
        credential = str(header_credential or body.get("credential") or "").strip() or None
        if credential:
            record = self.policy.consume_credential(credential)
            if record is None or record.capability_id != capability_id:
                return 403, {"ok": False, "error": {
                    "code": "invalid_credential",
                    "message": "一次性凭证无效（已用过或已过期）",
                }}
        else:
            decision = self.policy.check(session_id, capability_id, args)
            if decision["outcome"] == "deny":
                self.audit.append_event({"kind": "tool_exec", "session_id": session_id,
                                         "capability_id": capability_id, "ok": False,
                                         "error_code": "policy_denied"})
                return 403, {"ok": False, "error": {"code": "policy_denied", "message": decision["reason"]}}
            if decision["outcome"] == "confirmation_required":
                self.audit.append_event({"kind": "tool_exec", "session_id": session_id,
                                         "capability_id": capability_id, "ok": False,
                                         "error_code": "confirmation_required",
                                         "todo_id": decision.get("todo_id")})
                return 403, {"ok": False, "error": {
                    "code": "confirmation_required",
                    "message": decision["reason"],
                    "todo_id": decision.get("todo_id"),
                    "summary": decision.get("summary"),
                }}
        result = execute_capability(capability_id, args, self.tool_overrides)
        self.audit.append_event({
            "kind": "tool_exec", "session_id": session_id, "capability_id": capability_id,
            "ok": bool(result.get("ok")),
            "error_code": (result.get("error") or {}).get("code"),
            "credential_used": bool(credential),
        })
        if not result.get("ok"):
            error = result.get("error") or {"code": "tool_failed", "message": "unknown"}
            return 200, {"ok": False, "error": error}
        return 200, {"ok": True, "output": result.get("output")}

    # ---------- /internal/audit ----------
    def audit_event(self, body: dict) -> tuple[int, dict]:
        event = body.get("event")
        if not isinstance(event, dict):
            return 400, {"ok": False, "reason": "need_event_object"}
        todo_id = event.get("todo_id")
        if event.get("outcome") == "confirmation_required" and isinstance(todo_id, str) and todo_id:
            context = self.policy.confirmation_request(todo_id)
            if context is not None:
                request, decision_obj, at = context
                self.audit.record_confirmation(request, decision_obj, todo_id, at)
        return 200, {"ok": True, "recorded": self.audit.append_event(dict(event))}

    # ---------- /internal/bridge/worker_approval ----------
    def worker_approval(self, body: dict) -> tuple[int, dict]:
        task_id = str(body.get("task_id") or "").strip()
        capability = str(body.get("capability") or "").strip()
        args_summary = str(body.get("args_summary") or "").strip()
        todo_id = str(body.get("todo_id") or "").strip()
        if not (task_id and capability and todo_id):
            return 400, {"ok": False, "reason": "need_task_id_capability_todo_id"}
        callback = body.get("callback")
        run_id = body.get("run_id")
        entry = self.approvals.register(
            todo_id, "worker", f"{capability}：{args_summary}", "worker",
            capability, self.now(), task_id=task_id,
            callback=str(callback) if isinstance(callback, str) and callback.strip() else None,
            run_id=str(run_id) if isinstance(run_id, str) and run_id.strip() else None,
        )
        return 200, {"ok": True, "id": entry.id, "expiresAt": entry.as_latest()["expiresAt"]}

    # ---------- /internal/memory/search（Phase 4） ----------
    def memory_search(self, body: dict) -> tuple[int, dict]:
        text = body.get("query")
        if not isinstance(text, str):
            return 400, {"ok": False, "reason": "query_must_be_string"}
        people = body.get("people") or []
        types = body.get("types") or []
        limit = body.get("limit", 8)
        if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
            return 400, {"ok": False, "reason": "people_must_be_string_list"}
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            return 400, {"ok": False, "reason": "types_must_be_string_list"}
        if not isinstance(limit, int) or not 1 <= limit <= 8:
            return 400, {"ok": False, "reason": "limit_must_be_1_to_8"}
        packet = self.memory.search(
            text, people=tuple(people), types=tuple(types), limit=limit)
        return 200, {"ok": True, **MemoryService.packet_to_wire(packet)}

    # ---------- /latest、/decide（approval_ipc.py 同形） ----------
    def approvals_latest(self, query: dict) -> tuple[int, dict]:
        session = query.get("session") or None
        entry = self.approvals.latest(self.now(), session)
        return 200, {"ok": True, "pending": entry.as_latest() if entry else None}

    def approvals_decide(self, body: dict) -> tuple[int, dict]:
        pending_id = str(body.get("id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        if not pending_id or decision not in ("approve", "reject"):
            return 400, {"ok": False}
        entry = self.approvals.get(pending_id)
        if entry is None or entry.status != "open":
            return 409, {"ok": False}
        if entry.kind == "confirmation":
            try:
                self.policy.resolve_for_ipc(pending_id, decision)
            except ValueError:
                return 409, {"ok": False}
            self.audit.append_event({"kind": "approval_decide", "todo_id": pending_id,
                                     "decision": decision, "channel": "approval_ipc"})
            return 200, {"ok": True}
        decided = self.approvals.decide(pending_id, decision, self.now())
        if decided is None:
            return 409, {"ok": False}
        if decision == "approve" and entry.callback and entry.run_id:
            credential = self.policy.credential_for(pending_id, entry.capability)
            if not self._resume_worker(entry.callback, entry.run_id, decision, credential):
                return 502, {"ok": False, "reason": "callback_failed"}
        self.audit.append_event({"kind": "approval_decide", "todo_id": pending_id,
                                 "decision": decision, "channel": "approval_ipc"})
        return 200, {"ok": True}

    def _resume_worker(self, callback: str, run_id: str, decision: str, credential: str) -> bool:
        """决定回调 TS Runtime：POST {callback}/v1/worker/runs/{run_id}/resume。"""
        url = callback.rstrip("/") + f"/v1/worker/runs/{run_id}/resume"
        try:
            with httpx.Client(trust_env=False, timeout=5.0) as client:
                response = client.post(url, json={"decision": decision, "credential": credential})
            return response.status_code == 200
        except httpx.HTTPError:
            return False


def make_bridge_server(app: BridgeApp, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                       token: str | None = None, approval_cap: str | None = None) -> ThreadingHTTPServer:
    """建好但不启动；port=0 让系统挑空闲端口（测试用）。端口被占时抛 OSError。"""
    bearer = token if token is not None else resolve_token()
    cap = approval_cap if approval_cap is not None else resolve_approval_cap()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 不往 stderr 打访问日志
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, needs_bearer: bool) -> bool:
            """本机 + Host 防重绑定；Bearer 或（审批口）能力串任一通过即可。"""
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._send(403, {"ok": False, "reason": "not_local"})
                return False
            host_header = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host_header not in _LOCAL_HOSTS:
                self._send(403, {"ok": False, "reason": "bad_host"})
                return False
            auth = self.headers.get("Authorization") or ""
            if auth == f"Bearer {bearer}":
                return True
            if not needs_bearer:
                given_cap = self.headers.get("X-Guanjia-Approval-Cap") or ""
                if cap and given_cap and hmac.compare_digest(given_cap.encode(), cap.encode()):
                    return True
            self._send(403, {"ok": False, "reason": "bad_token"})
            return False

        def _json_body(self) -> dict | None:
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                self._send(415, {"ok": False, "reason": "need_json"})
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._send(400, {"ok": False, "reason": "bad_length"})
                return None
            if length > _MAX_BODY:
                self._send(413, {"ok": False, "reason": "too_large"})
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, UnicodeDecodeError):
                body = None
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "reason": "bad_json"})
                return None
            return body

        def do_GET(self):
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/latest", "/internal/approvals/latest"):
                if not self._guard(needs_bearer=False):
                    return
                self._send(*app.approvals_latest(query))
                return
            if url.path == "/status":
                if not self._guard(needs_bearer=True):
                    return
                self._send(200, {"ok": True, "service": "guanjia-bridge"})
                return
            self._send(404, {"ok": False, "reason": "not_found"})

        def do_POST(self):
            url = urlsplit(self.path)
            approval_route = url.path in ("/decide", "/internal/approvals/decide")
            if not self._guard(needs_bearer=not approval_route):
                return
            body = self._json_body()
            if body is None:
                return
            if url.path == "/internal/policy/check":
                self._send(*app.policy_check(body))
            elif url.path == "/internal/policy/confirm":
                self._send(*app.policy_confirm(body))
            elif url.path.startswith("/internal/tools/") and url.path.endswith("/exec"):
                capability_id = url.path[len("/internal/tools/"):-len("/exec")]
                self._send(*app.tools_exec(capability_id, body,
                                           self.headers.get("x-guanjia-credential")))
            elif url.path == "/internal/audit":
                self._send(*app.audit_event(body))
            elif url.path == "/internal/bridge/worker_approval":
                self._send(*app.worker_approval(body))
            elif url.path == "/internal/memory/search":
                self._send(*app.memory_search(body))
            elif approval_route:
                self._send(*app.approvals_decide(body))
            else:
                self._send(404, {"ok": False, "reason": "not_found"})

    class _ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address = False  # Windows 上 SO_REUSEADDR 会让第二个实例也绑上同一端口
        daemon_threads = True

        def server_bind(self) -> None:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    return _ExclusiveServer((host, port), Handler)


def start_bridge_server(app: BridgeApp, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                        token: str | None = None, approval_cap: str | None = None) -> ThreadingHTTPServer:
    """建好并在后台线程启动；端口被占时抛 OSError。"""
    server = make_bridge_server(app, port, host, token, approval_cap)
    threading.Thread(target=server.serve_forever, name="bridge", daemon=True).start()
    return server


def main() -> None:
    port = int(os.environ.get("GUANJIA_BRIDGE_PORT", str(DEFAULT_PORT)))
    bearer = resolve_token()
    cap = resolve_approval_cap()
    app = BridgeApp()
    server = make_bridge_server(app, port, token=bearer, approval_cap=cap)
    print(f"guanjia bridge on http://127.0.0.1:{port}", flush=True)
    if not os.environ.get("GUANJIA_BRIDGE_TOKEN"):
        try:
            json.loads((REPO_ROOT / "agent-runtime" / "config" / "runtime.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"warning: GUANJIA_BRIDGE_TOKEN 未设置且 runtime.json 缺失，本次使用随机 token：{bearer}",
                  file=sys.stderr, flush=True)
    if not os.environ.get("GUANJIA_APPROVAL_CAP"):
        print(f"approval cap for this run: {cap}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.store.close()
        app.memory.close()


if __name__ == "__main__":
    main()
