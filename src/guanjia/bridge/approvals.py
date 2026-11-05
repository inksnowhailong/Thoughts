"""审批待办注册表：confirmation 与 worker 两类 pending 的最新一条查询与决定。

与旧守卫 loopback 服务（openclaw/plugins/guanjia-guard/src/approval-ipc.ts）同形：
- GET  /latest  → {"ok": true, "pending": {id, summary, status, expiresAt} | null}
- POST /decide  {"id", "decision"} → {"ok": bool}，已决定/过期回 409
内存注册表：进程重启 pending 作废，与审批 120 秒超时语义一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock

PENDING_TTL_SEC = 120


@dataclass(frozen=True)
class PendingApproval:
    id: str
    kind: str  # 'confirmation' | 'worker'
    summary: str
    session_id: str
    task_id: str | None
    capability: str
    expires_at: datetime
    status: str = 'open'  # open | approved | rejected
    # worker 审批专用：决定后回调 TS Runtime 的地址与 run_id
    callback: str | None = None
    run_id: str | None = None
    # 注册序号：/latest 取最新注册的一条（expires_at 相同时按它排序）
    seq: int = 0

    def as_latest(self) -> dict:
        return {
            'id': self.id,
            'summary': self.summary,
            'status': self.status,
            'expiresAt': self.expires_at.isoformat().replace('+00:00', 'Z'),
        }


class ApprovalRegistry:
    def __init__(self, ttl_sec: int = PENDING_TTL_SEC) -> None:
        self._ttl = timedelta(seconds=ttl_sec)
        self._lock = RLock()
        self._entries: dict[str, PendingApproval] = {}
        self._seq = 0

    def register(self, entry_id: str, kind: str, summary: str, session_id: str,
                 capability: str, now: datetime, task_id: str | None = None,
                 callback: str | None = None, run_id: str | None = None) -> PendingApproval:
        with self._lock:
            self._seq += 1
            seq = self._seq
        entry = PendingApproval(
            entry_id, kind, summary, session_id, task_id, capability,
            now + self._ttl, callback=callback, run_id=run_id, seq=seq,
        )
        with self._lock:
            self._entries[entry_id] = entry
        return entry

    def get(self, entry_id: str) -> PendingApproval | None:
        with self._lock:
            return self._entries.get(entry_id)

    def latest(self, now: datetime, session_id: str | None = None) -> PendingApproval | None:
        """最新的未决定、未过期待审；可按会话过滤（同形旧接口的 ?session=）。"""
        with self._lock:
            candidates = [
                entry for entry in self._entries.values()
                if entry.status == 'open'
                and now < entry.expires_at
                and (session_id is None or entry.session_id == session_id)
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: (entry.expires_at, entry.seq))

    def decide(self, entry_id: str, decision: str, now: datetime) -> PendingApproval | None:
        """把 open 的待审置为 approved/rejected；已决定/不存在/已过期返回 None。"""
        if decision not in ('approve', 'reject'):
            return None
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None or entry.status != 'open' or now >= entry.expires_at:
                return None
            decided = PendingApproval(
                entry.id, entry.kind, entry.summary, entry.session_id, entry.task_id,
                entry.capability, entry.expires_at,
                'approved' if decision == 'approve' else 'rejected',
                entry.callback, entry.run_id, entry.seq,
            )
            self._entries[entry_id] = decided
        return decided
