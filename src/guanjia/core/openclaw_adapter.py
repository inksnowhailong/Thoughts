"""OpenClaw protocol adapter with durable logical-session mapping only."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from threading import Lock, RLock
from typing import Mapping, MutableMapping, Protocol, Sequence

from .main_event_parser import EventValidationError, freeze_json, thaw_json


class OpenClawTransport(Protocol):
    """The sole integration boundary used by the adapter."""

    def invoke(self, request: dict[str, object]) -> dict[str, object]: ...


class SessionMapStore(Protocol):
    def get_or_create(self, key: str, expected: str) -> str: ...


class SQLiteSessionMapStore:
    """Cross-process CAS-backed logical-session mapping."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = RLock()
        self._closed = False
        self._db.execute('CREATE TABLE IF NOT EXISTS openclaw_sessions (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def get_or_create(self, key: str, expected: str) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError('session map store is closed')
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._db.execute('INSERT OR IGNORE INTO openclaw_sessions VALUES (?, ?)', (key, expected))
                value = self._db.execute('SELECT value FROM openclaw_sessions WHERE key=?', (key,)).fetchone()[0]
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
        if value != expected:
            raise RuntimeError('session mapping conflict')
        return value

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True


class OpenClawAdapter:
    _namespace_re = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$')
    _segment_re = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
    _state_locks: dict[int, Lock] = {}
    _state_locks_guard = Lock()

    """Maps stable local logical names to opaque OpenClaw session identifiers."""

    def __init__(
        self,
        transport: OpenClawTransport,
        *,
        state: MutableMapping[str, str] | SessionMapStore | None = None,
        namespace: str = 'guanjia',
    ) -> None:
        if not self._namespace_re.fullmatch(namespace):
            raise ValueError('adapter namespace must be a simple identifier')
        self._transport = transport
        self._state = state if state is not None else {}
        self._namespace = namespace
        with self._state_locks_guard:
            self._state_lock = self._state_locks.setdefault(id(self._state), Lock())

    def session_for(self, logical_session: str) -> str:
        """Get or deterministically allocate an opaque external session key."""
        self._validate_logical_session(logical_session)
        key = f'{self._namespace}:{logical_session}'
        expected = f'oc:{self._namespace}:{sha256(key.encode("utf-8")).hexdigest()[:24]}'
        if hasattr(self._state, 'get_or_create'):
            return self._state.get_or_create(key, expected)  # type: ignore[union-attr]
        with self._state_lock:
            session = self._state.get(key)
            if session is None:
                self._state[key] = expected
                if self._state.get(key) != expected:
                    raise RuntimeError('session mapping compare-and-set conflict')
                return expected
            if session != expected:
                raise RuntimeError('session mapping conflict')
            return session

    def call(
        self,
        *,
        logical_session: str,
        model: str,
        command: str,
        operation_id: str | None = None,
        messages: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        """Forward only the canonical session/model/messages protocol."""
        if not isinstance(model, str) or not model.strip():
            raise ValueError('model must be a non-empty string')
        if not isinstance(command, str) or not command:
            raise ValueError('command must be a non-empty string')
        if operation_id is not None and not isinstance(operation_id, str):
            raise ValueError('operation_id must be a string')
        normalized_messages: list[dict[str, object]] = []
        for message in messages:
            if not isinstance(message, Mapping) or set(message) != {'role', 'content'}:
                raise ValueError('messages must have exactly role and content')
            if message['role'] not in {'system', 'user', 'assistant', 'tool'}:
                raise ValueError('unsupported message role')
            normalized_messages.append({
                'role': message['role'],
                'content': thaw_json(freeze_json(message['content'])),
            })
        response = self._transport.invoke({
            'session': self.session_for(logical_session),
            'model': model,
            'command': command,
            **({} if operation_id is None else {'operation_id': operation_id}),
            'messages': normalized_messages,
        })
        if not isinstance(response, Mapping) or set(response) - {'ok', 'request_id', 'error'}:
            raise ValueError('invalid OpenClaw response schema')
        if response.get('ok') is True and isinstance(response.get('request_id'), str):
            return dict(response)
        if response.get('ok') is False and isinstance(response.get('error'), str):
            return dict(response)
        raise ValueError('invalid OpenClaw response schema')

    @staticmethod
    def _validate_logical_session(value: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or not value
            or not all(OpenClawAdapter._segment_re.fullmatch(part) for part in value.split('/'))
        ):
            raise ValueError('logical session is unsafe')
