"""SQLite WAL 领域事件日志，提供显式事务和幂等追加。"""

from __future__ import annotations

import sqlite3
import threading
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from guanjia.contracts.events import EventEnvelope


class IdempotencyConflict(ValueError):
    """同一幂等键指向不同完整事件，不能静默合并。"""


@dataclass(frozen=True)
class StoredEvent:
    """带单调水位的事件记录。"""

    offset: int
    envelope: EventEnvelope
    created: bool


class EventStore:
    """路径由宿主注入的本地事件日志，不管理任何外部资源。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._depth = 0
        self._transaction_owner: int | None = None
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute('PRAGMA foreign_keys=ON')
        self._connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS event_log (
                offset INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                envelope_json TEXT NOT NULL
            )
            '''
        )
        self._connection.execute(
            'CREATE INDEX IF NOT EXISTS event_log_correlation ON event_log(correlation_id, offset)'
        )
        self._connection.execute(
            'CREATE INDEX IF NOT EXISTS event_log_type ON event_log(event_type, offset)'
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> 'EventStore':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """锁定连接，并将同线程嵌套上下文合并为一个 SQLite 事务。"""
        with self._lock:
            thread_id = threading.get_ident()
            outermost = self._depth == 0
            if not outermost and self._transaction_owner != thread_id:
                raise RuntimeError('nested event-store transaction must use its owner thread')
            if outermost:
                self._transaction_owner = thread_id
                try:
                    self._connection.execute('BEGIN IMMEDIATE')
                except BaseException:
                    self._reset_transaction()
                    raise
            self._depth += 1
            try:
                yield self._connection
            except BaseException:
                self._depth -= 1
                if outermost:
                    try:
                        self._connection.execute('ROLLBACK')
                    finally:
                        self._reset_transaction()
                raise
            else:
                self._depth -= 1
                if outermost:
                    try:
                        self._connection.execute('COMMIT')
                    except BaseException:
                        try:
                            self._connection.execute('ROLLBACK')
                        finally:
                            self._reset_transaction()
                        raise
                    self._reset_transaction()

    def _reset_transaction(self) -> None:
        self._depth = 0
        self._transaction_owner = None

    def append(self, envelope: EventEnvelope) -> StoredEvent:
        """幂等写入事件；相同幂等键返回最初持久化的记录。"""
        self._validate_command_envelope(envelope)
        with self.transaction() as connection:
            try:
                cursor = connection.execute(
                    '''
                    INSERT INTO event_log(
                        event_id, event_type, correlation_id, idempotency_key, envelope_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ''',
                    (
                        envelope.event_id,
                        envelope.event_type,
                        envelope.correlation_id,
                        envelope.idempotency_key,
                        envelope.to_json(),
                    ),
                )
                return StoredEvent(cursor.lastrowid, envelope, created=True)
            except sqlite3.IntegrityError:
                row = connection.execute(
                    'SELECT offset, envelope_json FROM event_log WHERE idempotency_key = ?',
                    (envelope.idempotency_key,),
                ).fetchone()
                if row is None:
                    event_id_row = connection.execute(
                        'SELECT offset FROM event_log WHERE event_id = ?', (envelope.event_id,)
                    ).fetchone()
                    if event_id_row is not None:
                        raise IdempotencyConflict(
                            f'event_id {envelope.event_id!r} is already associated with another command'
                        ) from None
                    raise
                stored = EventEnvelope.from_json(row['envelope_json'])
                if not self._same_command_event(stored, envelope):
                    raise IdempotencyConflict(
                        f'idempotency key {envelope.idempotency_key!r} has a different envelope'
                    )
                return StoredEvent(row['offset'], stored, created=False)

    @staticmethod
    def _validate_command_envelope(envelope: EventEnvelope) -> None:
        for field_name, value in (
            ('event_id', envelope.event_id),
            ('idempotency_key', envelope.idempotency_key),
        ):
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 200
                or re.fullmatch(r'[^\s\x00-\x1f]+', value) is None
            ):
                raise ValueError(f'{field_name} must be a safe identifier of at most 200 characters')
        if envelope.occurred_at.tzinfo is None or envelope.occurred_at.utcoffset() != timedelta(0):
            raise ValueError('event timestamp must be UTC')

    @staticmethod
    def _same_command_event(left: EventEnvelope, right: EventEnvelope) -> bool:
        """比较命令语义，忽略重放时重新生成的事件 ID 与时间戳。"""
        return (
            left.event_type == right.event_type
            and left.correlation_id == right.correlation_id
            and left.causation_id == right.causation_id
            and left.idempotency_key == right.idempotency_key
            and left.schema_version == right.schema_version
            and left.payload == right.payload
        )

    def read(
        self,
        *,
        correlation_id: str | None = None,
        event_type: str | None = None,
        after_watermark: int = 0,
        limit: int = 1000,
    ) -> tuple[StoredEvent, ...]:
        """按相关 ID、事件类型和水位读取有序事件。"""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be an integer between 1 and 1000')
        if type(after_watermark) is not int or after_watermark < 0:
            raise ValueError('after_watermark must be a non-negative integer')
        if correlation_id is not None and (not isinstance(correlation_id, str) or not correlation_id):
            raise ValueError('correlation_id must be a non-empty string')
        if event_type is not None and (not isinstance(event_type, str) or not event_type):
            raise ValueError('event_type must be a non-empty string')
        clauses = ['offset > ?']
        values: list[object] = [after_watermark]
        if correlation_id is not None:
            clauses.append('correlation_id = ?')
            values.append(correlation_id)
        if event_type is not None:
            clauses.append('event_type = ?')
            values.append(event_type)
        query = f"SELECT offset, envelope_json FROM event_log WHERE {' AND '.join(clauses)} ORDER BY offset"
        query += ' LIMIT ?'
        values.append(limit)
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        return tuple(
            StoredEvent(row['offset'], EventEnvelope.from_json(row['envelope_json']), created=False)
            for row in rows
        )

    def watermark(self) -> int:
        """返回已提交日志的最大水位。"""
        with self._lock:
            row = self._connection.execute(
                'SELECT COALESCE(MAX(offset), 0) AS offset FROM event_log'
            ).fetchone()
        return int(row['offset'])
