"""SQLite metadata store for input records and their current processing result."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from guanjia.contracts import EventId

from .contracts import CurrentProcessingResult, InputMessage, StateContext
from .confidence import replacement_decision

SCHEMA_VERSION = 2


class IdempotencyConflict(ValueError):
    """An input ID was replayed with metadata that is not byte-for-byte equivalent."""


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('timestamp must be an aware UTC datetime')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + '+00:00')


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return _timestamp(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    if hasattr(value, 'items'):
        return dict(value.items())  # type: ignore[union-attr]
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f'{type(value).__name__} is not JSON serializable')


def _message_payload(message: InputMessage) -> str:
    return json.dumps(message, default=_json_default, ensure_ascii=False, sort_keys=True)


class InputStore:
    """Owns only input metadata; the database path is always caller-injectable."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute('PRAGMA foreign_keys=ON')
        self._initialize()

    def _initialize(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                'CREATE TABLE IF NOT EXISTS input_schema (version INTEGER NOT NULL)'
            )
            version = connection.execute('SELECT version FROM input_schema').fetchone()
            if version is None:
                connection.execute('INSERT INTO input_schema(version) VALUES (?)', (SCHEMA_VERSION,))
            elif version['version'] == 1:
                connection.execute('UPDATE input_schema SET version = ?', (SCHEMA_VERSION,))
            elif version['version'] != SCHEMA_VERSION:
                raise RuntimeError(f'unsupported input schema version: {version["version"]}')
            connection.execute(
                '''CREATE TABLE IF NOT EXISTS input_messages (
                    input_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    message_json TEXT NOT NULL,
                    stored_at TEXT NOT NULL
                )'''
            )
            connection.execute(
                '''CREATE TABLE IF NOT EXISTS current_processing_results (
                    input_id TEXT PRIMARY KEY REFERENCES input_messages(input_id) ON DELETE CASCADE,
                    processor_name TEXT NOT NULL,
                    processor_version TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    processed_at TEXT NOT NULL,
                    review_required INTEGER NOT NULL
                )'''
            )
            connection.execute(
                '''CREATE TABLE IF NOT EXISTS input_delivery (
                    input_id TEXT PRIMARY KEY REFERENCES input_messages(input_id) ON DELETE CASCADE,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    lease_until TEXT,
                    lease_token TEXT,
                    last_error TEXT
                )'''
            )
            connection.execute(
                '''CREATE TABLE IF NOT EXISTS input_state_contexts (
                    context_ref TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL
                )'''
            )
            columns = {
                row['name'] for row in connection.execute('PRAGMA table_info(input_delivery)')
            }
            if 'lease_token' not in columns:
                connection.execute('ALTER TABLE input_delivery ADD COLUMN lease_token TEXT')

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute('SELECT version FROM input_schema').fetchone()
        return int(row['version'])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Expose a small atomic API for higher-level input operations."""
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def store_only(self, message: InputMessage) -> bool:
        """Persist a non-deliverable input only; complete messages use save_and_enqueue."""
        message_json = _message_payload(message)
        with self.transaction() as connection:
            existing = connection.execute(
                'SELECT message_json FROM input_messages WHERE input_id = ?', (str(message.input_id),)
            ).fetchone()
            if existing is not None:
                if existing['message_json'] != message_json:
                    raise IdempotencyConflict(f'input_id {message.input_id} has different content')
                return False
            connection.execute(
                '''INSERT INTO input_messages
                    (input_id, source, received_at, message_json, stored_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (str(message.input_id), message.source, _timestamp(message.received_at), message_json,
                 _timestamp(datetime.now(timezone.utc))),
            )
        return True

    def save_input(self, message: InputMessage) -> bool:
        raise RuntimeError('save_input is not valid for deliverable input; use save_and_enqueue')

    def save_state_context(self, context_ref: str, context: StateContext) -> bool:
        """Persist an immutable context snapshot before any message references it."""
        if not isinstance(context_ref, str) or not context_ref.strip():
            raise ValueError('context_ref must be a non-empty string')
        if not isinstance(context, StateContext):
            raise TypeError('context must be a StateContext')
        payload = json.dumps(context, default=_json_default, ensure_ascii=False, sort_keys=True)
        with self.transaction() as connection:
            existing = connection.execute(
                'SELECT context_json FROM input_state_contexts WHERE context_ref = ?', (context_ref,)
            ).fetchone()
            if existing is not None:
                if existing['context_json'] != payload:
                    raise IdempotencyConflict(f'state context {context_ref} has different content')
                return False
            connection.execute(
                'INSERT INTO input_state_contexts(context_ref, context_json) VALUES (?, ?)',
                (context_ref, payload),
            )
        return True

    def save_and_enqueue(self, message: InputMessage, payload: str, now: datetime) -> bool:
        """Atomically persist input metadata and its durable outbox message."""
        if not isinstance(payload, str):
            raise ValueError('payload must be a string')
        available_at = _timestamp(now)
        message_json = _message_payload(message)
        with self.transaction() as connection:
            if message.state_context_ref is not None and connection.execute(
                'SELECT 1 FROM input_state_contexts WHERE context_ref = ?',
                (message.state_context_ref,),
            ).fetchone() is None:
                raise KeyError(f'unknown state_context_ref: {message.state_context_ref}')
            existing = connection.execute(
                'SELECT message_json FROM input_messages WHERE input_id = ?', (str(message.input_id),)
            ).fetchone()
            inserted = existing is None
            if existing is not None:
                if existing['message_json'] != message_json:
                    raise IdempotencyConflict(f'input_id {message.input_id} has different content')
            else:
                connection.execute(
                    '''INSERT INTO input_messages(input_id, source, received_at, message_json, stored_at)
                       VALUES (?, ?, ?, ?, ?)''',
                    (str(message.input_id), message.source, _timestamp(message.received_at), message_json,
                     _timestamp(datetime.now(timezone.utc))),
                )
            queued = connection.execute(
                'SELECT payload FROM input_delivery WHERE input_id = ?', (str(message.input_id),)
            ).fetchone()
            if queued is not None:
                if queued['payload'] != payload:
                    raise IdempotencyConflict(f'input_id {message.input_id} has different delivery payload')
            else:
                connection.execute(
                    'INSERT INTO input_delivery(input_id, payload, available_at) VALUES (?, ?, ?)',
                    (
                        str(message.input_id),
                        payload,
                        _timestamp(now + timedelta(seconds=1)),
                    ),
                )
        return inserted

    def has_input(self, input_id: EventId | str) -> bool:
        with self._lock:
            return self._connection.execute(
                'SELECT 1 FROM input_messages WHERE input_id = ?', (str(input_id),)
            ).fetchone() is not None

    def raw_input(self, input_id: EventId | str) -> dict[str, object] | None:
        """Return the stored metadata payload without coupling callers to SQLite."""
        with self._lock:
            row = self._connection.execute(
                'SELECT message_json FROM input_messages WHERE input_id = ?', (str(input_id),)
            ).fetchone()
        return None if row is None else json.loads(row['message_json'])

    def set_current_processing_result(
        self, result: CurrentProcessingResult, *, margin: float = 0.05
    ) -> bool:
        """CAS-replace current result; older or close candidates never silently overwrite."""
        with self.transaction() as connection:
            if connection.execute(
                'SELECT 1 FROM input_messages WHERE input_id = ?', (str(result.input_id),)
            ).fetchone() is None:
                raise KeyError(f'unknown input_id: {result.input_id}')
            existing = connection.execute(
                'SELECT * FROM current_processing_results WHERE input_id = ?', (str(result.input_id),)
            ).fetchone()
            if existing is not None:
                current_at = _parse_timestamp(existing['processed_at'])
                fields_json = json.dumps(result.fields, default=_json_default, sort_keys=True)
                if result.processed_at < current_at:
                    return False
                if result.processed_at == current_at:
                    same = (
                        existing['processor_name'] == result.processor_name
                        and existing['processor_version'] == result.processor_version
                        and existing['fields_json'] == fields_json
                        and existing['confidence'] == result.confidence
                    )
                    if not same:
                        raise IdempotencyConflict('processing results conflict at identical processed_at')
                    return False
                replace, review = replacement_decision(
                    existing['confidence'], result.confidence, margin=margin
                )
                if not replace:
                    if review:
                        connection.execute(
                            'UPDATE current_processing_results SET review_required = 1 WHERE input_id = ?',
                            (str(result.input_id),),
                        )
                    return False
            connection.execute(
                '''INSERT INTO current_processing_results
                    (input_id, processor_name, processor_version, fields_json, confidence,
                     processed_at, review_required)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(input_id) DO UPDATE SET
                     processor_name=excluded.processor_name,
                     processor_version=excluded.processor_version,
                     fields_json=excluded.fields_json,
                     confidence=excluded.confidence,
                     processed_at=excluded.processed_at,
                     review_required=excluded.review_required''',
                (
                    str(result.input_id),
                    result.processor_name,
                    result.processor_version,
                    json.dumps(result.fields, default=_json_default, sort_keys=True),
                    result.confidence,
                    _timestamp(result.processed_at),
                    int(result.review_required),
                ),
            )
        return True

    def get_current_processing_result(
        self, input_id: EventId | str
    ) -> CurrentProcessingResult | None:
        with self._lock:
            row = self._connection.execute(
                'SELECT * FROM current_processing_results WHERE input_id = ?', (str(input_id),)
            ).fetchone()
        if row is None:
            return None
        return CurrentProcessingResult(
            input_id=EventId(row['input_id']),
            processor_name=row['processor_name'],
            processor_version=row['processor_version'],
            fields=json.loads(row['fields_json']),
            confidence=row['confidence'],
            processed_at=_parse_timestamp(row['processed_at']),
            review_required=bool(row['review_required']),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
