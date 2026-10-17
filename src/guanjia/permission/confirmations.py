"""跨渠道确认待办与并发安全存储。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import RLock
import sqlite3
import re
from typing import Protocol

from .contracts import ActorEvidence, AuthorizationRequest, Decision, Reversibility, required, utc
from .contracts import IdempotencyConflict
from .roles import Role


class ConfirmationStatus(str, Enum):
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    DEFERRED = 'deferred'
    UNCERTAIN = 'uncertain'
    EXPIRED = 'expired'


class MigrationRequired(RuntimeError):
    """Existing durable permission state cannot be inferred safely."""


@dataclass(frozen=True)
class ConfirmationTodo:
    todo_id: str
    request_id: str
    actor_id: str
    actor_role: Role
    session_id: str
    task_id: str | None
    intent: str
    capability: str
    object_scope: str
    risk_scope: str
    action_fingerprint: str
    reversibility: Reversibility
    within_intent_scope: bool
    expires_at: datetime
    status: ConfirmationStatus = ConfirmationStatus.PENDING

    def __post_init__(self) -> None:
        for value, name in (
            (self.todo_id, 'todo_id'), (self.request_id, 'request_id'), (self.actor_id, 'actor_id'),
            (self.session_id, 'session_id'), (self.intent, 'intent'),
            (self.capability, 'capability'), (self.object_scope, 'object_scope'),
            (self.risk_scope, 'risk_scope'), (self.action_fingerprint, 'action_fingerprint'),
        ):
            required(value, name)
        if self.task_id is not None:
            required(self.task_id, 'task_id')
        if type(self.actor_role) is not Role or type(self.status) is not ConfirmationStatus or type(self.reversibility) is not Reversibility:
            raise ValueError('todo role or status is invalid')
        if type(self.within_intent_scope) is not bool:
            raise ValueError('within_intent_scope must be bool')
        if re.fullmatch(r'[0-9a-f]{64}', self.action_fingerprint) is None:
            raise ValueError('action_fingerprint must be a lowercase 64-hex string')
        utc(self.expires_at, 'expires_at')

    @classmethod
    def from_request(cls, todo_id: str, request: AuthorizationRequest, expires_at: datetime) -> 'ConfirmationTodo':
        return cls(
            todo_id, request.request_id, request.actor.actor_id, request.actor.role, request.session_id,
            request.task_id, request.intent, request.capability, request.object_scope,
            request.risk_scope, request.action_fingerprint, request.reversibility,
            request.within_intent_scope, expires_at,
        )

    def same_request(self, request: AuthorizationRequest) -> bool:
        return (
            self.request_id == request.request_id
            and self.actor_id == request.actor.actor_id
            and self.actor_role == request.actor.role
            and self.session_id == request.session_id
            and self.task_id == request.task_id
            and self.intent == request.intent
            and self.capability == request.capability
            and self.object_scope == request.object_scope
            and self.risk_scope == request.risk_scope
            and self.action_fingerprint == request.action_fingerprint
            and self.reversibility is request.reversibility
            and self.within_intent_scope is request.within_intent_scope
        )

    def matches(self, request: AuthorizationRequest) -> bool:
        return (
            self.status is ConfirmationStatus.APPROVED
            and request.now < self.expires_at
            and self.same_request(request)
        )

    def resolve(self, actor: ActorEvidence, decision: Decision, now: datetime) -> 'ConfirmationTodo':
        if type(decision) is not Decision:
            raise ValueError('permission layer only accepts structured decisions')
        if actor.actor_id != self.actor_id or actor.role != self.actor_role:
            raise ValueError('actor cannot resolve this confirmation todo')
        utc(now, 'now')
        if now >= self.expires_at:
            if self.status is ConfirmationStatus.EXPIRED:
                return self
            return ConfirmationTodo(**{**asdict(self), 'status': ConfirmationStatus.EXPIRED})
        target = {
            Decision.APPROVE: ConfirmationStatus.APPROVED, Decision.REJECT: ConfirmationStatus.REJECTED,
            Decision.DEFER: ConfirmationStatus.DEFERRED, Decision.UNCERTAIN: ConfirmationStatus.UNCERTAIN,
        }[decision]
        if self.status is target:
            return self
        if self.status in (ConfirmationStatus.APPROVED, ConfirmationStatus.REJECTED, ConfirmationStatus.EXPIRED):
            raise ValueError('terminal confirmation todo cannot transition')
        return ConfirmationTodo(**{**asdict(self), 'status': target})


class InMemoryPermissionStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._todos: dict[str, ConfirmationTodo] = {}
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError('permission store is closed')

    def save_todo(self, todo: ConfirmationTodo) -> None:
        with self._lock:
            self._ensure_open()
            existing = self._todos.get(todo.todo_id)
            if existing is not None:
                if existing == todo:
                    return
                raise IdempotencyConflict('confirmation todo id already belongs to different semantics')
            existing_request = self.todo_for_request(todo.request_id)
            if existing_request is not None:
                raise IdempotencyConflict('request id already belongs to a different confirmation todo')
            self._todos[todo.todo_id] = todo

    def resolve(
        self, todo_id: str, actor: ActorEvidence, decision: Decision, now: datetime
    ) -> ConfirmationTodo:
        required(todo_id, 'todo_id')
        with self._lock:
            self._ensure_open()
            try:
                todo = self._todos[todo_id]
            except KeyError as exc:
                raise KeyError(todo_id) from exc
            resolved = todo.resolve(actor, decision, now)
            self._todos[todo_id] = resolved
            return resolved

    def active_todos_for(
        self, actor_id: str, actor_role: Role, session_id: str, task_id: str | None, now: datetime
    ) -> tuple[ConfirmationTodo, ...]:
        required(actor_id, 'actor_id')
        required(session_id, 'session_id')
        if task_id is not None:
            required(task_id, 'task_id')
        if type(actor_role) is not Role:
            raise ValueError('actor_role must be a Role')
        utc(now, 'now')
        with self._lock:
            self._ensure_open()
            return tuple(
                todo for todo in self._todos.values()
                if todo.actor_id == actor_id
                and todo.actor_role is actor_role
                and todo.session_id == session_id
                and todo.task_id == task_id
                and now < todo.expires_at
                and todo.status in (
                    ConfirmationStatus.PENDING, ConfirmationStatus.DEFERRED, ConfirmationStatus.UNCERTAIN,
                )
            )

    def approved_for(
        self, actor_id: str, actor_role: Role, session_id: str, task_id: str | None, now: datetime
    ) -> tuple[ConfirmationTodo, ...]:
        required(actor_id, 'actor_id')
        required(session_id, 'session_id')
        if task_id is not None:
            required(task_id, 'task_id')
        if type(actor_role) is not Role:
            raise ValueError('actor_role must be a Role')
        utc(now, 'now')
        with self._lock:
            self._ensure_open()
            return tuple(
                todo for todo in self._todos.values()
                if todo.actor_id == actor_id
                and todo.actor_role is actor_role
                and todo.session_id == session_id
                and todo.task_id == task_id
                and now < todo.expires_at
                and todo.status is ConfirmationStatus.APPROVED
            )

    def decisions_for(self, request: AuthorizationRequest) -> tuple[ConfirmationTodo, ...]:
        with self._lock:
            self._ensure_open()
            return self.approved_for(
                request.actor.actor_id, request.actor.role, request.session_id, request.task_id, request.now
            )

    def todo(self, todo_id: str) -> ConfirmationTodo | None:
        required(todo_id, 'todo_id')
        with self._lock:
            self._ensure_open()
            return self._todos.get(todo_id)

    def todo_for_request(self, request_id: str) -> ConfirmationTodo | None:
        required(request_id, 'request_id')
        with self._lock:
            self._ensure_open()
            return next((todo for todo in self._todos.values() if todo.request_id == request_id), None)

    def close(self) -> None:
        """Keep the in-memory store interchangeable with durable stores."""

        with self._lock:
            self._closed = True


class PermissionStore(Protocol):
    def save_todo(self, todo: ConfirmationTodo) -> None: ...
    def resolve(self, todo_id: str, actor: ActorEvidence, decision: Decision, now: datetime) -> ConfirmationTodo: ...
    def decisions_for(self, request: AuthorizationRequest) -> tuple[ConfirmationTodo, ...]: ...
    def todo(self, todo_id: str) -> ConfirmationTodo | None: ...
    def todo_for_request(self, request_id: str) -> ConfirmationTodo | None: ...
    def close(self) -> None: ...


class SQLitePermissionStore:
    """Durable todo store using WAL; callers may recreate it after a restart."""

    _SCHEMA_VERSION = 1
    _COLUMNS = (
        ('todo_id', 'TEXT'), ('request_id', 'TEXT'), ('actor_id', 'TEXT'),
        ('actor_role', 'TEXT'), ('session_id', 'TEXT'), ('task_id', 'TEXT'),
        ('intent', 'TEXT'), ('capability', 'TEXT'), ('object_scope', 'TEXT'),
        ('risk_scope', 'TEXT'), ('action_fingerprint', 'TEXT'),
        ('reversibility', 'TEXT'), ('within_intent_scope', 'INTEGER'),
        ('expires_at', 'TEXT'), ('status', 'TEXT'), ('version', 'INTEGER'),
    )

    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            str(database_path), timeout=5, check_same_thread=False, isolation_level=None,
        )
        self._closed = False
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute('PRAGMA busy_timeout=5000')
        try:
            self._initialize_schema()
        except BaseException:
            self.close()
            raise

    def _initialize_schema(self) -> None:
        version = int(self._connection.execute('PRAGMA user_version').fetchone()[0])
        table_exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_todos'"
        ).fetchone() is not None
        if version > self._SCHEMA_VERSION:
            raise ValueError('permission store schema is newer than this application')
        if not table_exists:
            if version != 0:
                raise MigrationRequired('permission todo table is missing for its schema version')
            self._create_v1_table()
            self._connection.execute(f'PRAGMA user_version={self._SCHEMA_VERSION}')
            return

        columns = tuple(
            (str(row[1]), str(row[2]).upper())
            for row in self._connection.execute('PRAGMA table_info(permission_todos)').fetchall()
        )
        if columns == self._COLUMNS:
            try:
                self._connection.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS permission_todos_request_id '
                    'ON permission_todos(request_id)'
                )
            except sqlite3.IntegrityError as exc:
                raise MigrationRequired('permission todo request ids are not unique') from exc
            if version == 0:
                self._connection.execute(f'PRAGMA user_version={self._SCHEMA_VERSION}')
            return

        safe_legacy_columns = self._COLUMNS[:-1]
        if version == 0 and columns == safe_legacy_columns:
            self._migrate_v0_without_version()
            return
        raise MigrationRequired('permission todo schema cannot be migrated without losing semantics')

    def _create_v1_table(self) -> None:
        self._connection.execute(
            '''CREATE TABLE IF NOT EXISTS permission_todos (
                todo_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                session_id TEXT NOT NULL,
                task_id TEXT,
                intent TEXT NOT NULL,
                capability TEXT NOT NULL,
                object_scope TEXT NOT NULL,
                risk_scope TEXT NOT NULL,
                action_fingerprint TEXT NOT NULL,
                reversibility TEXT NOT NULL,
                within_intent_scope INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL
            )'''
        )
        self._connection.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS permission_todos_request_id '
            'ON permission_todos(request_id)'
        )

    def _migrate_v0_without_version(self) -> None:
        field_names = ', '.join(name for name, _ in self._COLUMNS[:-1])
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            rows = self._connection.execute(
                f'SELECT {field_names}, 1 AS version FROM permission_todos'
            ).fetchall()
            for row in rows:
                self._row_to_todo(row)
            self._connection.execute('ALTER TABLE permission_todos RENAME TO permission_todos_legacy')
            self._create_v1_table()
            self._connection.execute(
                f'INSERT INTO permission_todos ({field_names}, version) '
                f'SELECT {field_names}, 1 FROM permission_todos_legacy'
            )
            self._connection.execute('DROP TABLE permission_todos_legacy')
            self._connection.execute(f'PRAGMA user_version={self._SCHEMA_VERSION}')
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError('permission store is closed')

    @staticmethod
    def _row_to_todo(row: tuple[object, ...]) -> ConfirmationTodo:
        try:
            if len(row) != 16:
                raise ValueError('permission todo row has an invalid field count')
            text_indexes = (0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 13, 14)
            if any(type(row[index]) is not str for index in text_indexes):
                raise ValueError('permission todo text fields must be strings')
            if row[5] is not None and type(row[5]) is not str:
                raise ValueError('permission todo task_id must be a string or null')
            if type(row[12]) is not int or row[12] not in (0, 1):
                raise ValueError('within_intent_scope must be stored as 0 or 1')
            if type(row[15]) is not int or row[15] < 1:
                raise ValueError('permission todo version is invalid')
            expires_at = datetime.fromisoformat(row[13])
            utc(expires_at, 'expires_at')
            if expires_at.isoformat() != row[13]:
                raise ValueError('expires_at must be canonical UTC ISO-8601')
            return ConfirmationTodo(
                todo_id=row[0], request_id=row[1], actor_id=row[2],
                actor_role=Role(row[3]), session_id=row[4], task_id=row[5],
                intent=row[6], capability=row[7], object_scope=row[8], risk_scope=row[9],
                action_fingerprint=row[10], reversibility=Reversibility(row[11]),
                within_intent_scope=bool(row[12]), expires_at=expires_at,
                status=ConfirmationStatus(row[14]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError('invalid permission todo row') from exc

    @staticmethod
    def _values(todo: ConfirmationTodo) -> tuple[object, ...]:
        return (
            todo.todo_id, todo.request_id, todo.actor_id, todo.actor_role.value,
            todo.session_id, todo.task_id, todo.intent, todo.capability, todo.object_scope,
            todo.risk_scope, todo.action_fingerprint, todo.reversibility.value,
            int(todo.within_intent_scope), todo.expires_at.isoformat(), todo.status.value,
        )

    def _fetch_todo(self, todo_id: str) -> tuple[ConfirmationTodo, int] | None:
        row = self._connection.execute(
            'SELECT todo_id, request_id, actor_id, actor_role, session_id, task_id, intent, '
            'capability, object_scope, risk_scope, action_fingerprint, reversibility, '
            'within_intent_scope, expires_at, status, version FROM permission_todos WHERE todo_id=?',
            (todo_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_todo(row), int(row[15])

    def _fetch_request(self, request_id: str) -> tuple[ConfirmationTodo, int] | None:
        row = self._connection.execute(
            'SELECT todo_id, request_id, actor_id, actor_role, session_id, task_id, intent, '
            'capability, object_scope, risk_scope, action_fingerprint, reversibility, '
            'within_intent_scope, expires_at, status, version FROM permission_todos WHERE request_id=?',
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_todo(row), int(row[15])

    def save_todo(self, todo: ConfirmationTodo) -> None:
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                existing = self._fetch_todo(todo.todo_id)
                existing_request = self._fetch_request(todo.request_id)
                if existing is None and existing_request is None:
                    self._connection.execute(
                        'INSERT INTO permission_todos (todo_id, request_id, actor_id, actor_role, '
                        'session_id, task_id, intent, capability, object_scope, risk_scope, '
                        'action_fingerprint, reversibility, within_intent_scope, expires_at, status, version) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)',
                        self._values(todo),
                    )
                elif existing is not None and existing[0] != todo:
                    raise IdempotencyConflict('confirmation todo id already belongs to different semantics')
                elif existing_request is not None and existing_request[0] != todo:
                    raise IdempotencyConflict('request id already belongs to a different confirmation todo')
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def resolve(self, todo_id: str, actor: ActorEvidence, decision: Decision, now: datetime) -> ConfirmationTodo:
        required(todo_id, 'todo_id')
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                existing = self._fetch_todo(todo_id)
                if existing is None:
                    raise KeyError(todo_id)
                todo, version = existing
                resolved = todo.resolve(actor, decision, now)
                if resolved != todo:
                    cursor = self._connection.execute(
                        'UPDATE permission_todos SET status=?, version=version+1 WHERE todo_id=? AND version=?',
                        (resolved.status.value, todo_id, version),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError('confirmation todo was modified concurrently')
                self._connection.commit()
                return resolved
            except BaseException:
                self._connection.rollback()
                raise

    def decisions_for(self, request: AuthorizationRequest) -> tuple[ConfirmationTodo, ...]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                'SELECT todo_id, request_id, actor_id, actor_role, session_id, task_id, intent, '
                'capability, object_scope, risk_scope, action_fingerprint, reversibility, '
                'within_intent_scope, expires_at, status, version FROM permission_todos '
                'WHERE actor_id=? AND actor_role=? AND session_id=? '
                'AND task_id IS ? AND status=? AND expires_at>?',
                (
                    request.actor.actor_id, request.actor.role.value, request.session_id,
                    request.task_id, ConfirmationStatus.APPROVED.value,
                    request.now.isoformat(),
                ),
            ).fetchall()
            todos = tuple(self._row_to_todo(row) for row in rows)
            return tuple(todo for todo in todos if todo.matches(request))

    def todo(self, todo_id: str) -> ConfirmationTodo | None:
        required(todo_id, 'todo_id')
        with self._lock:
            self._ensure_open()
            result = self._fetch_todo(todo_id)
            return None if result is None else result[0]

    def todo_for_request(self, request_id: str) -> ConfirmationTodo | None:
        required(request_id, 'request_id')
        with self._lock:
            self._ensure_open()
            result = self._fetch_request(request_id)
            return None if result is None else result[0]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True
