"""bridge 审计：confirmation 走 permission/audit.py 的结构化审计，其余事件落 JSONL。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from guanjia.permission.audit import PermissionAudit
from guanjia.permission.contracts import AuthorizationRequest, PermissionDecision


class BridgeAudit:
    def __init__(self, permission_audit: PermissionAudit | None, jsonl_path: Path | None) -> None:
        self._permission_audit = permission_audit
        self._jsonl_path = jsonl_path

    def record_confirmation(self, request: AuthorizationRequest,
                            decision: PermissionDecision, todo_id: str, now: datetime) -> None:
        """confirmation_required 的结构化审计（permission/audit.py 的 PermissionAudit.record）。"""
        if self._permission_audit is None:
            return
        self._permission_audit.record(request, decision, todo_id, now)

    def append_event(self, event: dict) -> str:
        """普通执行/拒绝事件的 JSONL 审计；返回落盘位置描述。"""
        if self._jsonl_path is None:
            return 'memory-only'
        line = {'t': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **event}
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._jsonl_path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + '\n')
        except OSError:
            return 'jsonl-write-failed'
        return 'jsonl'
