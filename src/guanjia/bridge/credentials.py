"""一次性审批凭证：内存借条，用后即焚。

语义（与审批 120 秒超时一致，见 CONFIRMATION_TIMEOUT_SEC）：
- approve 确认时签发 credential（uuid4）并绑定 todo/session/capability/指纹；
- exec 消费时校验「存在、未用过、未过期」，校验通过立刻标已用；
- 进程重启即作废（内存字典，pending 同理）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

CONFIRMATION_TIMEOUT_SEC = 120


@dataclass(frozen=True)
class CredentialRecord:
    credential: str
    todo_id: str
    session_id: str
    capability_id: str
    action_fingerprint: str
    expires_at: datetime
    used: bool = False


class CredentialLedger:
    def __init__(self, timeout_sec: int = CONFIRMATION_TIMEOUT_SEC) -> None:
        self._timeout = timedelta(seconds=timeout_sec)
        self._lock = RLock()
        self._records: dict[str, CredentialRecord] = {}

    def issue(self, todo_id: str, session_id: str, capability_id: str,
              action_fingerprint: str, now: datetime) -> str:
        credential = str(uuid.uuid4())
        with self._lock:
            self._records[credential] = CredentialRecord(
                credential, todo_id, session_id, capability_id, action_fingerprint,
                now + self._timeout,
            )
        return credential

    def peek(self, credential: str, now: datetime) -> CredentialRecord | None:
        """只看不用（policy/check 带 credential 时用）；过期/已用一律 None。"""
        with self._lock:
            record = self._records.get(credential)
        if record is None or record.used or now >= record.expires_at:
            return None
        return record

    def consume(self, credential: str, now: datetime) -> CredentialRecord | None:
        """一次性消费：校验通过立刻标已用；再次消费同一张即失效。"""
        with self._lock:
            record = self._records.get(credential)
            if record is None or record.used or now >= record.expires_at:
                return None
            used = CredentialRecord(
                record.credential, record.todo_id, record.session_id,
                record.capability_id, record.action_fingerprint, record.expires_at, True,
            )
            self._records[credential] = used
        return record
