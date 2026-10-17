"""Durable, passive audit records for cross-channel confirmation displays."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Protocol

from .contracts import AuthorizationRequest, IdempotencyConflict, PermissionDecision, PermissionOutcome, required, utc

UI_DISPLAY_TTL = timedelta(minutes=3)
LATCH_TTL = timedelta(minutes=10)


def action_fingerprint(request: AuthorizationRequest) -> str:
    """Use only Wave 1's opaque action fingerprint; never derive a second algorithm."""

    value = request.action_fingerprint
    if not isinstance(value, str):
        raise ValueError('action_fingerprint must be a string')
    required(value, 'action_fingerprint')
    if re.fullmatch(r'[a-fA-F0-9]{64}', value) is None:
        raise ValueError('action_fingerprint must be an opaque SHA-256 hex value')
    return value.lower()


@dataclass(frozen=True)
class PermissionAuditRecord:
    """A record with deliberately separate UI and approval-latch lifetimes."""

    record_id: str
    todo_id: str
    actor_id: str
    session_id: str
    task_id: str | None
    action_fingerprint: str
    outcome: PermissionOutcome
    recorded_at: datetime
    display_expires_at: datetime
    latch_expires_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.record_id, 'record_id'),
            (self.todo_id, 'todo_id'),
            (self.actor_id, 'actor_id'),
            (self.session_id, 'session_id'),
            (self.action_fingerprint, 'action_fingerprint'),
            (self.outcome, 'outcome'),
        ):
            required(value, name)
        if self.task_id is not None:
            required(self.task_id, 'task_id')
        if type(self.outcome) is not PermissionOutcome:
            raise ValueError('outcome must be a PermissionOutcome')
        for value, name in (
            (self.recorded_at, 'recorded_at'),
            (self.display_expires_at, 'display_expires_at'),
            (self.latch_expires_at, 'latch_expires_at'),
        ):
            utc(value, name)
        if self.display_expires_at != self.recorded_at + UI_DISPLAY_TTL:
            raise ValueError('display_expires_at must use the 3 minute UI TTL')
        if self.latch_expires_at != self.recorded_at + LATCH_TTL:
            raise ValueError('latch_expires_at must use the 10 minute latch TTL')


def _same_semantics(left: PermissionAuditRecord, right: PermissionAuditRecord) -> bool:
    """Timestamps describe the first observation and are intentionally not replay semantics."""

    return (
        left.record_id == right.record_id
        and left.todo_id == right.todo_id
        and left.actor_id == right.actor_id
        and left.session_id == right.session_id
        and left.task_id == right.task_id
        and left.action_fingerprint == right.action_fingerprint
        and left.outcome is right.outcome
    )


class PermissionAuditStore(Protocol):
    """Persistent host-provided storage with atomic create-if-absent semantics."""

    def save_if_absent(self, record: PermissionAuditRecord) -> bool:
        """Atomically save once or reject an existing ID with changed semantics."""

    def get(self, record_id: str) -> PermissionAuditRecord | None:
        """Return a persisted record by its opaque stable identifier."""


class InMemoryPermissionAuditStore:
    """Concurrency-safe reference implementation; reuse the instance after restart."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, PermissionAuditRecord] = {}

    def save_if_absent(self, record: PermissionAuditRecord) -> bool:
        with self._lock:
            existing = self._records.get(record.record_id)
            if existing is None:
                self._records[record.record_id] = record
                return True
            if not _same_semantics(existing, record):
                raise IdempotencyConflict('permission audit id conflicts with different semantics')
            return False

    def get(self, record_id: str) -> PermissionAuditRecord | None:
        with self._lock:
            return self._records.get(record_id)


class SQLitePermissionAuditStore:
    """SQLite-backed audit evidence only; it has no approval or latch-consumption API."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS permission_audit (
                    record_id TEXT PRIMARY KEY,
                    todo_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT,
                    action_fingerprint TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    display_expires_at TEXT NOT NULL,
                    latch_expires_at TEXT NOT NULL
                )
                '''
            )
            connection.commit()
        finally:
            connection.close()

    def save_if_absent(self, record: PermissionAuditRecord) -> bool:
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            existing = self._read(connection, record.record_id)
            if existing is not None:
                if not _same_semantics(existing, record):
                    raise IdempotencyConflict('permission audit id conflicts with different semantics')
                connection.commit()
                return False
            connection.execute(
                '''
                INSERT INTO permission_audit (
                    record_id, todo_id, actor_id, session_id, task_id,
                    action_fingerprint, outcome, recorded_at,
                    display_expires_at, latch_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                self._values(record),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, record_id: str) -> PermissionAuditRecord | None:
        required(record_id, 'record_id')
        connection = self._connect()
        try:
            return self._read(connection, record_id)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=5000')
        return connection

    @staticmethod
    def _values(record: PermissionAuditRecord) -> tuple[str, str, str, str, str | None, str, str, str, str, str]:
        return (
            record.record_id,
            record.todo_id,
            record.actor_id,
            record.session_id,
            record.task_id,
            record.action_fingerprint,
            record.outcome.value,
            _utc_text(record.recorded_at),
            _utc_text(record.display_expires_at),
            _utc_text(record.latch_expires_at),
        )

    @staticmethod
    def _read(connection: sqlite3.Connection, record_id: str) -> PermissionAuditRecord | None:
        row = connection.execute(
            '''
            SELECT record_id, todo_id, actor_id, session_id, task_id,
                   action_fingerprint, outcome, recorded_at,
                   display_expires_at, latch_expires_at
            FROM permission_audit WHERE record_id = ?
            ''',
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return PermissionAuditRecord(
                record_id=row[0],
                todo_id=row[1],
                actor_id=row[2],
                session_id=row[3],
                task_id=row[4],
                action_fingerprint=row[5],
                outcome=PermissionOutcome(row[6]),
                recorded_at=_decode_utc(row[7], 'recorded_at'),
                display_expires_at=_decode_utc(row[8], 'display_expires_at'),
                latch_expires_at=_decode_utc(row[9], 'latch_expires_at'),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError('invalid persisted permission audit record') from exc


def _utc_text(value: datetime) -> str:
    utc(value, 'timestamp')
    return value.isoformat().replace('+00:00', 'Z')


def _decode_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError(f'{name} must be a UTC timestamp')
    parsed = datetime.fromisoformat(value[:-1] + '+00:00')
    utc(parsed, name)
    return parsed


class PermissionAudit:
    """Records and queries audit evidence; it does not approve or release work."""

    def __init__(self, store: PermissionAuditStore) -> None:
        self._store = store

    def record(
        self,
        request: AuthorizationRequest,
        decision: PermissionDecision,
        todo_id: str,
        now: datetime,
    ) -> tuple[PermissionAuditRecord, bool]:
        if not isinstance(request, AuthorizationRequest) or not isinstance(decision, PermissionDecision):
            raise TypeError('request and decision must use permission domain contracts')
        required(todo_id, 'todo_id')
        utc(now, 'now')
        if decision.outcome is not PermissionOutcome.CONFIRMATION_REQUIRED:
            raise ValueError('permission audit only records confirmation_required decisions')
        if decision.todo_id != todo_id:
            raise ValueError('decision todo_id must match audit todo_id')
        fingerprint = action_fingerprint(request)
        identity = '\0'.join((
            todo_id,
            request.actor.actor_id,
            request.session_id,
            request.task_id or '',
            fingerprint,
        ))
        record_id = sha256(identity.encode('utf-8')).hexdigest()
        existing = self._store.get(record_id)
        if existing is not None:
            candidate = PermissionAuditRecord(
                record_id=record_id,
                todo_id=todo_id,
                actor_id=request.actor.actor_id,
                session_id=request.session_id,
                task_id=request.task_id,
                action_fingerprint=fingerprint,
                outcome=decision.outcome,
                recorded_at=existing.recorded_at,
                display_expires_at=existing.display_expires_at,
                latch_expires_at=existing.latch_expires_at,
            )
            if not _same_semantics(existing, candidate):
                raise IdempotencyConflict('permission audit id belongs to different semantics')
            return existing, False
        record = PermissionAuditRecord(
            record_id=record_id,
            todo_id=todo_id,
            actor_id=request.actor.actor_id,
            session_id=request.session_id,
            task_id=request.task_id,
            action_fingerprint=fingerprint,
            outcome=decision.outcome,
            recorded_at=now,
            display_expires_at=now + UI_DISPLAY_TTL,
            latch_expires_at=now + LATCH_TTL,
        )
        created = self._store.save_if_absent(record)
        if created:
            return record, True
        persisted = self._store.get(record_id)
        if persisted is None:
            raise RuntimeError('permission audit store lost an existing record')
        if not _same_semantics(persisted, record):
            raise IdempotencyConflict('permission audit id belongs to different semantics')
        return persisted, False

    def confirmation_for(
        self,
        *,
        session_id: str,
        actor_id: str,
        todo_id: str,
        task_id: str | None,
        action_fingerprint: str,
        now: datetime,
    ) -> PermissionAuditRecord | None:
        """Query only the exact session, actor, todo, task, and action binding."""

        for value, name in (
            (session_id, 'session_id'),
            (actor_id, 'actor_id'),
            (todo_id, 'todo_id'),
            (action_fingerprint, 'action_fingerprint'),
        ):
            required(value, name)
        if task_id is not None:
            required(task_id, 'task_id')
        utc(now, 'now')
        action_fingerprint = action_fingerprint.lower()
        record_id = sha256('\0'.join((todo_id, actor_id, session_id, task_id or '', action_fingerprint)).encode('utf-8')).hexdigest()
        record = self._store.get(record_id)
        if record is None:
            return None
        if (
            record.session_id != session_id
            or record.actor_id != actor_id
            or record.todo_id != todo_id
            or record.task_id != task_id
            or record.action_fingerprint != action_fingerprint
        ):
            return None
        return record if now < record.latch_expires_at else None

    @staticmethod
    def visible_in_ui(record: PermissionAuditRecord, now: datetime) -> bool:
        utc(now, 'now')
        return now < record.display_expires_at

    @staticmethod
    def latch_active(record: PermissionAuditRecord, now: datetime) -> bool:
        utc(now, 'now')
        return now < record.latch_expires_at


__all__ = [
    'InMemoryPermissionAuditStore',
    'LATCH_TTL',
    'PermissionAudit',
    'PermissionAuditRecord',
    'PermissionAuditStore',
    'SQLitePermissionAuditStore',
    'UI_DISPLAY_TTL',
    'action_fingerprint',
]
