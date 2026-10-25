"""守护进程任务 IPC：仅 loopback + 一次性 capability，绝不暴露任务目标或会话键。"""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from guanjia.task_manager import TaskError
from guanjia.task_service import TaskService


TASK_DESCRIPTOR = Path.home() / ".openclaw" / "workspace" / "guanjia" / "task-ipc.json"
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _parse_when(when: Any) -> float | None:
    """把 when 字段解析为 epoch 秒；缺省/无效/已过时都返回 None（立即执行）。"""
    if not isinstance(when, str) or not when.strip():
        return None
    from datetime import datetime
    text = when.strip()
    try:
        # 支持 "2026-10-25T20:00" / "2026-10-25 20:00" / "2026-10-25T20:00:00"（本地时间）
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    import time as _time
    epoch = parsed.timestamp()
    return epoch if epoch > _time.time() else None


def _public(task: dict[str, Any]) -> dict[str, Any]:
    """跨进程只给工具层任务摘要，目标和 worker session 始终留在守护进程内。"""
    keys = ("id", "summary", "status", "progress", "progress_percent", "result", "error", "updated_at", "run_at")
    item = {key: task[key] for key in keys if key in task}
    if task.get("approval"):
        item["approval"] = {"id": task["approval"]["id"], "description": task["approval"]["description"],
                            "expires_at": task["approval"].get("expires_at")}
    return item


class TaskIpc:
    """仅 O4 受控工具使用的本机接口；capability 不写入任务审计或返回值。"""

    def __init__(self, tasks: TaskService, descriptor: Path = TASK_DESCRIPTOR,
                 on_started: Callable[[dict[str, Any]], None] | None = None,
                 on_cancelled: Callable[[str, str], None] | None = None,
                 on_approval_decision: Callable[[str, str], None] | None = None) -> None:
        self.tasks, self.descriptor = tasks, descriptor
        self.on_started = on_started
        self.on_cancelled = on_cancelled
        self.on_approval_decision = on_approval_decision
        self._capability = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                return self.client_address[0] in ("127.0.0.1", "::1") and secrets.compare_digest(
                    self.headers.get("X-Guanjia-Task-Cap", ""), owner._capability)

            def _body(self) -> dict[str, Any] | None:
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, UnicodeDecodeError):
                    return None
                return body if isinstance(body, dict) else None

            def do_GET(self) -> None:
                if not self._authorized():
                    self._send(403, {"ok": False})
                    return
                query = parse_qs(urlsplit(self.path).query)
                if urlsplit(self.path).path != "/status":
                    self._send(404, {"ok": False})
                    return
                task_id = (query.get("id") or [""])[0]
                try:
                    data = _public(owner.tasks.task_status(task_id)) if task_id else owner.tasks.task_status()
                except TaskError:
                    self._send(404, {"ok": False})
                    return
                self._send(200, {"ok": True, "task": data} if task_id else {"ok": True, "summary": data})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._send(403, {"ok": False})
                    return
                body = self._body()
                if body is None:
                    self._send(400, {"ok": False})
                    return
                path = urlsplit(self.path).path
                try:
                    if path == "/start":
                        summary, goal = body.get("summary"), body.get("goal")
                        if not isinstance(summary, str) or not isinstance(goal, str) or len(summary) > 120 or len(goal) > 1000:
                            raise TaskError("bad_input")
                        run_at = _parse_when(body.get("when"))
                        task = owner.tasks.task_start(summary, goal, run_at=run_at)
                        if owner.on_started is not None:
                            try:
                                owner.on_started(task)
                            except Exception:
                                # 调度登记失败不影响安全的「只创建 queued」语义。
                                pass
                    else:
                        task_id = body.get("id")
                        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
                            raise TaskError("bad_id")
                        if path == "/cancel":
                            reason = body.get("reason", "")
                            if not isinstance(reason, str) or len(reason) > 200:
                                raise TaskError("bad_input")
                            if owner.on_cancelled is not None:
                                owner.on_cancelled(task_id, reason)
                            else:
                                task = owner.tasks.task_cancel(task_id, reason)
                            task = owner.tasks.task_status(task_id)
                        elif path == "/report":
                            progress, percent = body.get("progress"), body.get("percent")
                            if not isinstance(progress, str) or len(progress) > 200 or (percent is not None and not isinstance(percent, (int, float))):
                                raise TaskError("bad_input")
                            task = owner.tasks.task_report(task_id, progress, percent)
                        elif path == "/approval":
                            approval_id, description, expires_at = body.get("approval_id"), body.get("description"), body.get("expires_at")
                            if (not isinstance(approval_id, str) or not approval_id or not isinstance(description, str) or not description or
                                    len(description) > 200 or (expires_at is not None and not isinstance(expires_at, (int, float)))):
                                raise TaskError("bad_input")
                            task = owner.tasks.task_waiting_approval(task_id, approval_id, description, expires_at)
                        elif path == "/approval-decision":
                            decision = body.get("decision")
                            if decision not in ("approve", "reject") or owner.on_approval_decision is None:
                                raise TaskError("bad_input")
                            owner.on_approval_decision(task_id, decision)
                            task = owner.tasks.task_status(task_id)
                        else:
                            self._send(404, {"ok": False})
                            return
                except TaskError:
                    self._send(409, {"ok": False})
                    return
                self._send(200, {"ok": True, "task": _public(task)})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.descriptor.parent.mkdir(parents=True, exist_ok=True)
        self.descriptor.write_text(json.dumps({"port": port, "capability": self._capability}), encoding="utf-8")
        threading.Thread(target=self._server.serve_forever, name="task-ipc", daemon=True).start()

    def stop(self) -> None:
        try:
            self.descriptor.unlink(missing_ok=True)
        finally:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._server = None
