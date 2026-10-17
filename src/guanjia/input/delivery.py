"""Persistent at-least-once delivery queue for completed input messages."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .store import SCHEMA_VERSION


_BACKOFF_SECONDS = (1, 5, 25)


def _text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('timestamp must be an aware UTC datetime')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + '+00:00')


@dataclass(frozen=True)
class DeliveryItem:
    input_id: str
    payload: str
    attempts: int
    lease_until: datetime
    lease_token: str


class DeliveryQueue:
    """A lease queue whose replay keeps the original input ID."""

    def __init__(self, database_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute('PRAGMA journal_mode=WAL')
        schema = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='input_schema'"
        ).fetchone()
        if schema is None:
            self._connection.close()
            raise RuntimeError('InputStore must initialize the database before DeliveryQueue')
        version = self._connection.execute('SELECT version FROM input_schema').fetchone()['version']
        required = {'input_id', 'payload', 'attempts', 'available_at', 'lease_until', 'lease_token'}
        columns = {row['name'] for row in self._connection.execute('PRAGMA table_info(input_delivery)')}
        if version != SCHEMA_VERSION or not required.issubset(columns):
            self._connection.close()
            raise RuntimeError('incompatible InputStore outbox schema')
        self._connection.execute(
            '''CREATE TABLE IF NOT EXISTS failed_input_delivery (
                input_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                failed_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )'''
        )
        self._connection.commit()

    def enqueue(self, input_id: str, payload: str, *, now: datetime) -> bool:
        raise RuntimeError('enqueue bypasses input durability; use InputStore.save_and_enqueue')

    def lease_next(self, *, now: datetime, lease_seconds: float = 30) -> DeliveryItem | None:
        """Lease one due record; expired leases naturally become available again."""
        if lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive')
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                row = self._connection.execute(
                    '''SELECT * FROM input_delivery
                       WHERE available_at <= ?
                         AND (lease_until IS NULL OR lease_until <= ?)
                       ORDER BY available_at, input_id LIMIT 1''',
                    (_text(now), _text(now)),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                lease_until = now + timedelta(seconds=lease_seconds)
                lease_token = str(uuid.uuid4())
                self._connection.execute(
                    'UPDATE input_delivery SET lease_until = ?, lease_token = ? WHERE input_id = ?',
                    (_text(lease_until), lease_token, row['input_id']),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return DeliveryItem(row['input_id'], row['payload'], row['attempts'], lease_until, lease_token)

    def acknowledge(self, item: DeliveryItem) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                'DELETE FROM input_delivery WHERE input_id = ? AND lease_until = ? AND lease_token = ?',
                (item.input_id, _text(item.lease_until), item.lease_token),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def fail(self, item: DeliveryItem, reason: str, *, now: datetime) -> None:
        """Schedule fixed 1/5/25-second retries, then atomically move to failure."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('reason must be a non-empty string')
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                row = self._connection.execute(
                    '''SELECT attempts, payload FROM input_delivery
                       WHERE input_id = ? AND lease_until = ? AND lease_token = ?''',
                    (item.input_id, _text(item.lease_until), item.lease_token),
                ).fetchone()
                if row is None:
                    raise ValueError('delivery lease is no longer held')
                attempts = row['attempts'] + 1
                if attempts >= 3:
                    self._connection.execute(
                        '''INSERT OR REPLACE INTO failed_input_delivery
                           (input_id, payload, attempts, failed_at, reason) VALUES (?, ?, ?, ?, ?)''',
                        (item.input_id, row['payload'], attempts, _text(now), reason),
                    )
                    self._connection.execute('DELETE FROM input_delivery WHERE input_id = ?', (item.input_id,))
                else:
                    retry_at = now + timedelta(seconds=_BACKOFF_SECONDS[attempts])
                    self._connection.execute(
                        '''UPDATE input_delivery
                           SET attempts = ?, available_at = ?, lease_until = NULL, lease_token = NULL, last_error = ?
                           WHERE input_id = ?''',
                        (attempts, _text(retry_at), reason, item.input_id),
                    )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def replay(self, input_id: str, *, now: datetime) -> bool:
        """Return a failed input to the queue under exactly the same ID."""
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                row = self._connection.execute(
                    'SELECT payload FROM failed_input_delivery WHERE input_id = ?', (input_id,)
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return False
                self._connection.execute('DELETE FROM failed_input_delivery WHERE input_id = ?', (input_id,))
                self._connection.execute(
                    '''INSERT INTO input_delivery(input_id, payload, attempts, available_at, lease_until, lease_token)
                       VALUES (?, ?, 0, ?, NULL, NULL)''',
                    (input_id, row['payload'], _text(now)),
                )
                self._connection.commit()
                return True
            except BaseException:
                self._connection.rollback()
                raise

    def failed(self, input_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                'SELECT * FROM failed_input_delivery WHERE input_id = ?', (input_id,)
            ).fetchone()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
