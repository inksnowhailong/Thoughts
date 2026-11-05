"""Repository facade with injectable in-memory and SQLite storage backends."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Protocol

from .confidence import ConfidenceCalibrator, IdentityCalibrator
from .contracts import (
    ConflictCase,
    EvidenceRef,
    MediaRef,
    MemoryCandidate,
    MemoryPacket,
    MemoryQuery,
    MemoryRecord,
    MemorySource,
    RetentionLevel,
)
from .conflicts import decide_update


class MemoryBackend(Protocol):
    def apply(self, candidate: MemoryCandidate, calibrator: ConfidenceCalibrator) -> MemoryRecord: ...
    def get(self, key: str) -> MemoryRecord | None: ...
    def put(self, record: MemoryRecord) -> None: ...
    def all(self) -> tuple[MemoryRecord, ...]: ...
    def add_conflict(self, conflict: ConflictCase) -> None: ...
    def conflicts(self, unresolved_only: bool) -> tuple[ConflictCase, ...]: ...
    def resolve_conflict(self, conflict_id: str, use_incoming: bool) -> MemoryRecord: ...
    def mark_used(self, record_id: str, at: datetime) -> MemoryRecord: ...
    def close(self) -> None: ...


class InMemoryBackend:
    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._conflicts: dict[str, ConflictCase] = {}
        self._lock = RLock()

    def get(self, key: str) -> MemoryRecord | None:
        with self._lock:
            return self._records.get(key)

    def apply(self, candidate: MemoryCandidate, calibrator: ConfidenceCalibrator) -> MemoryRecord:
        with self._lock:
            existing = self._records.get(candidate.key)
            if existing is None:
                record = MemoryRecord.from_candidate(candidate)
            elif _candidate_data(candidate) == _candidate_data(_as_candidate(existing)):
                return existing
            else:
                decision = decide_update(existing, candidate, calibrator)
                record = decision.record
                if record != existing:
                    self._drop_conflicts_for_key(candidate.key)
                if decision.conflict is not None:
                    conflict = replace(decision.conflict, current_hash=_record_hash(existing))
                    if not any(
                        item.key == candidate.key
                        and item.current_hash == conflict.current_hash
                        and _candidate_data(item.incoming) == _candidate_data(candidate)
                        for item in self._conflicts.values()
                    ):
                        self._conflicts[conflict.id] = conflict
            self._records[record.key] = record
            return record

    def _drop_conflicts_for_key(self, key: str) -> None:
        for conflict_id, conflict in tuple(self._conflicts.items()):
            if conflict.key == key:
                del self._conflicts[conflict_id]

    def put(self, record: MemoryRecord) -> None:
        with self._lock:
            self._records[record.key] = record

    def all(self) -> tuple[MemoryRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def add_conflict(self, conflict: ConflictCase) -> None:
        with self._lock:
            self._conflicts[conflict.id] = conflict

    def conflicts(self, unresolved_only: bool) -> tuple[ConflictCase, ...]:
        with self._lock:
            values = tuple(self._conflicts.values())
        return tuple(item for item in values if not unresolved_only or item.status == 'uncertain')

    def resolve_conflict(self, conflict_id: str, use_incoming: bool) -> MemoryRecord:
        with self._lock:
            conflict = self._conflicts[conflict_id]
            current = self._records.get(conflict.key)
            if current is None or current != conflict.current or _record_hash(current) != conflict.current_hash:
                del self._conflicts[conflict_id]
                raise ValueError('conflict is stale')
            record = conflict.current.replace_with(conflict.incoming) if use_incoming else conflict.current
            self._records[record.key] = record
            del self._conflicts[conflict_id]
            return record

    def mark_used(self, record_id: str, at: datetime) -> MemoryRecord:
        with self._lock:
            for record in self._records.values():
                if record.id == record_id:
                    used = replace(record, use_count=record.use_count + 1, last_used_at=at)
                    self._records[used.key] = used
                    return used
        raise KeyError(record_id)

    def close(self) -> None:
        return None


class SQLiteBackend:
    """SQLite storage with explicit ownership of transaction and connection lifecycle."""

    def __init__(self, connection: sqlite3.Connection, *, manage_connection: bool) -> None:
        self._connection = connection
        self._manage_connection = manage_connection
        self._lock = RLock()
        if manage_connection:
            self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA busy_timeout=5000')
        self._connection.execute(
            'CREATE TABLE IF NOT EXISTS guanjia_memories ('
            'key TEXT PRIMARY KEY, payload TEXT NOT NULL)'
        )
        self._connection.execute(
            'CREATE TABLE IF NOT EXISTS guanjia_memory_conflicts ('
            'id TEXT PRIMARY KEY, key TEXT NOT NULL DEFAULT "", current_hash TEXT NOT NULL DEFAULT "", '
            'status TEXT NOT NULL, payload TEXT NOT NULL)'
        )
        columns = {row[1] for row in self._connection.execute('PRAGMA table_info(guanjia_memory_conflicts)')}
        if 'key' not in columns:
            self._connection.execute('ALTER TABLE guanjia_memory_conflicts ADD COLUMN key TEXT NOT NULL DEFAULT ""')
        if 'current_hash' not in columns:
            self._connection.execute('ALTER TABLE guanjia_memory_conflicts ADD COLUMN current_hash TEXT NOT NULL DEFAULT ""')
        self._connection.execute(
            'CREATE INDEX IF NOT EXISTS guanjia_memory_conflicts_key_hash '
            'ON guanjia_memory_conflicts(key, current_hash, status)'
        )
        for conflict_id, payload in self._connection.execute(
            'SELECT id, payload FROM guanjia_memory_conflicts WHERE key = "" OR current_hash = ""'
        ):
            conflict = _decode_conflict(payload)
            self._connection.execute(
                'UPDATE guanjia_memory_conflicts SET key = ?, current_hash = ? WHERE id = ?',
                (conflict.key, conflict.current_hash, conflict_id),
            )
        if manage_connection:
            self._connection.commit()

    def _write(self, operation: object) -> object:
        try:
            if self._manage_connection:
                result = operation()
                self._connection.commit()
                return result
            self._connection.execute('SAVEPOINT guanjia_memory')
            try:
                result = operation()
                self._connection.execute('RELEASE SAVEPOINT guanjia_memory')
                return result
            except BaseException:
                self._connection.execute('ROLLBACK TO SAVEPOINT guanjia_memory')
                self._connection.execute('RELEASE SAVEPOINT guanjia_memory')
                raise
        except BaseException:
            if self._manage_connection:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
            raise

    def _atomic(self, operation: object) -> object:
        """Run a complete read/decide/write transition under one SQLite transaction."""
        try:
            if self._manage_connection:
                self._connection.execute('BEGIN IMMEDIATE')
                result = operation()
                self._connection.commit()
                return result
            self._connection.execute('SAVEPOINT guanjia_memory_atomic')
            try:
                result = operation()
                self._connection.execute('RELEASE SAVEPOINT guanjia_memory_atomic')
                return result
            except BaseException:
                self._connection.execute('ROLLBACK TO SAVEPOINT guanjia_memory_atomic')
                self._connection.execute('RELEASE SAVEPOINT guanjia_memory_atomic')
                raise
        except BaseException:
            if self._manage_connection:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
            raise

    def apply(self, candidate: MemoryCandidate, calibrator: ConfidenceCalibrator) -> MemoryRecord:
        with self._lock:
            def operation() -> MemoryRecord:
                row = self._connection.execute(
                    'SELECT payload FROM guanjia_memories WHERE key = ?', (candidate.key,)
                ).fetchone()
                existing = None if row is None else _decode(row[0])
                if existing is None:
                    record = MemoryRecord.from_candidate(candidate)
                elif _candidate_data(candidate) == _candidate_data(_as_candidate(existing)):
                    return existing
                else:
                    decision = decide_update(existing, candidate, calibrator)
                    record = decision.record
                    if record != existing:
                        self._connection.execute(
                            'DELETE FROM guanjia_memory_conflicts WHERE key = ?',
                            (candidate.key,),
                        )
                    if decision.conflict is not None:
                        conflict = replace(decision.conflict, current_hash=_record_hash(existing))
                        duplicates = tuple(
                            _decode_conflict(item[0])
                            for item in self._connection.execute(
                                'SELECT payload FROM guanjia_memory_conflicts '
                                'WHERE key = ? AND current_hash = ? AND status = ?',
                                (candidate.key, conflict.current_hash, conflict.status),
                            )
                        )
                        if not any(_candidate_data(item.incoming) == _candidate_data(candidate) for item in duplicates):
                            self._connection.execute(
                                'INSERT INTO guanjia_memory_conflicts(id, key, current_hash, status, payload) '
                                'VALUES(?, ?, ?, ?, ?)',
                                (conflict.id, conflict.key, conflict.current_hash, conflict.status, _encode_conflict(conflict)),
                            )
                self._connection.execute(
                    'INSERT INTO guanjia_memories(key, payload) VALUES(?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                    (record.key, _encode(record)),
                )
                return record

            return self._atomic(operation)  # type: ignore[return-value]

    def get(self, key: str) -> MemoryRecord | None:
        with self._lock:
            row = self._connection.execute(
                'SELECT payload FROM guanjia_memories WHERE key = ?', (key,)
            ).fetchone()
        return None if row is None else _decode(row[0])

    def put(self, record: MemoryRecord) -> None:
        with self._lock:
            self._write(lambda: self._connection.execute(
                'INSERT INTO guanjia_memories(key, payload) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                (record.key, _encode(record)),
            ))

    def all(self) -> tuple[MemoryRecord, ...]:
        with self._lock:
            rows = tuple(self._connection.execute('SELECT payload FROM guanjia_memories'))
        return tuple(_decode(row[0]) for row in rows)

    def add_conflict(self, conflict: ConflictCase) -> None:
        with self._lock:
            self._write(lambda: self._connection.execute(
                'INSERT OR REPLACE INTO guanjia_memory_conflicts(id, key, current_hash, status, payload) VALUES(?, ?, ?, ?, ?)',
                (conflict.id, conflict.key, conflict.current_hash, conflict.status, _encode_conflict(conflict)),
            ))

    def conflicts(self, unresolved_only: bool) -> tuple[ConflictCase, ...]:
        sql = 'SELECT payload FROM guanjia_memory_conflicts' + (' WHERE status = ?' if unresolved_only else '')
        args = ('uncertain',) if unresolved_only else ()
        with self._lock:
            rows = tuple(self._connection.execute(sql, args))
        return tuple(_decode_conflict(row[0]) for row in rows)

    def resolve_conflict(self, conflict_id: str, use_incoming: bool) -> MemoryRecord:
        with self._lock:
            def resolve() -> MemoryRecord:
                row = self._connection.execute(
                    'SELECT payload FROM guanjia_memory_conflicts WHERE id = ?', (conflict_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(conflict_id)
                conflict = _decode_conflict(row[0])
                current_row = self._connection.execute(
                    'SELECT payload FROM guanjia_memories WHERE key = ?', (conflict.key,)
                ).fetchone()
                current = None if current_row is None else _decode(current_row[0])
                if current is None or current != conflict.current or _record_hash(current) != conflict.current_hash:
                    self._connection.execute('DELETE FROM guanjia_memory_conflicts WHERE id = ?', (conflict_id,))
                    raise ValueError('conflict is stale')
                record = conflict.current.replace_with(conflict.incoming) if use_incoming else conflict.current
                self._connection.execute(
                    'INSERT INTO guanjia_memories(key, payload) VALUES(?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                    (record.key, _encode(record)),
                )
                self._connection.execute('DELETE FROM guanjia_memory_conflicts WHERE id = ?', (conflict_id,))
                return record
            return self._atomic(resolve)  # type: ignore[return-value]

    def mark_used(self, record_id: str, at: datetime) -> MemoryRecord:
        with self._lock:
            row = self._connection.execute(
                'SELECT key, payload FROM guanjia_memories'
            ).fetchall()
            for _, payload in row:
                record = _decode(payload)
                if record.id == record_id:
                    used = replace(record, use_count=record.use_count + 1, last_used_at=at)
                    self._write(lambda: self._connection.execute(
                        'UPDATE guanjia_memories SET payload = ? WHERE key = ?',
                        (_encode(used), used.key),
                    ))
                    return used
        raise KeyError(record_id)

    def close(self) -> None:
        if self._manage_connection:
            with self._lock:
                self._connection.close()


def _encode(record: MemoryRecord) -> str:
    return json.dumps(
        {
            'id': record.id,
            'key': record.key,
            'content': record.content,
            'memory_type': record.memory_type,
            'source': record.source.value,
            'confidence': record.confidence,
            'importance': record.importance,
            'retention': record.retention.value,
            'people': record.people,
            'evidence': [
                {
                    'reference': item.reference,
                    'source': item.source.value,
                    'observed_at': item.observed_at.isoformat() if item.observed_at else None,
                }
                for item in record.evidence
            ],
            'media': [
                {
                    'media_id': item.media_id,
                    'media_type': item.media_type,
                    'content_hash': item.content_hash,
                    'metadata': dict(item.metadata),
                }
                for item in record.media
            ],
            'conflict_key': record.conflict_key,
            'created_at': record.created_at.isoformat(),
            'updated_at': record.updated_at.isoformat(),
            'last_used_at': record.last_used_at.isoformat() if record.last_used_at else None,
            'use_count': record.use_count,
        },
        ensure_ascii=False,
        separators=(',', ':'),
    )


def _record_hash(record: MemoryRecord) -> str:
    """Fingerprint the exact persisted current value used to create a conflict."""
    return hashlib.sha256(_encode(record).encode('utf-8')).hexdigest()


def _datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f'{name} must be an ISO aware UTC datetime')
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} must be an ISO aware UTC datetime') from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f'{name} must be an ISO aware UTC datetime')
    return parsed.astimezone(timezone.utc)


def _decode(payload: str) -> MemoryRecord:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid memory payload') from error
    if not isinstance(data, dict):
        raise ValueError('invalid memory payload')
    return MemoryRecord(
        id=data['id'],
        key=data['key'],
        content=data['content'],
        memory_type=data['memory_type'],
        source=MemorySource(data['source']),
        confidence=data['confidence'],
        importance=data['importance'],
        retention=RetentionLevel(data['retention']),
        people=tuple(data['people']),
        evidence=tuple(
            EvidenceRef(
                item['reference'],
                MemorySource(item['source']),
                _datetime(item['observed_at'], 'observed_at') if item['observed_at'] else None,
            )
            for item in data['evidence']
        ),
        media=tuple(
            MediaRef(item['media_id'], item['media_type'], item['content_hash'], item['metadata'])
            for item in data['media']
        ),
        conflict_key=data.get('conflict_key'),
        created_at=_datetime(data['created_at'], 'created_at'),
        updated_at=_datetime(data['updated_at'], 'updated_at'),
        last_used_at=_datetime(data['last_used_at'], 'last_used_at') if data['last_used_at'] else None,
        use_count=data['use_count'],
    )


def _candidate_data(candidate: MemoryCandidate) -> dict[str, object]:
    return {
        'key': candidate.key,
        'content': candidate.content,
        'memory_type': candidate.memory_type,
        'source': candidate.source.value,
        'confidence': candidate.confidence,
        'importance': candidate.importance,
        'retention': candidate.retention.value,
        'people': candidate.people,
        'evidence': [
            {'reference': item.reference, 'source': item.source.value,
             'observed_at': item.observed_at.isoformat() if item.observed_at else None}
            for item in candidate.evidence
        ],
        'media': [
            {'media_id': item.media_id, 'media_type': item.media_type,
             'content_hash': item.content_hash, 'metadata': dict(item.metadata)}
            for item in candidate.media
        ],
        'conflict_key': candidate.conflict_key,
    }


def _as_candidate(record: MemoryRecord) -> MemoryCandidate:
    return MemoryCandidate(
        record.key, record.content, record.memory_type, record.source,
        record.confidence, record.importance, record.retention, record.people,
        record.evidence, record.media, record.conflict_key,
    )


def _candidate_from_data(data: dict[str, object]) -> MemoryCandidate:
    return MemoryCandidate(
        key=data['key'], content=data['content'], memory_type=data['memory_type'],
        source=MemorySource(data['source']), confidence=data['confidence'],
        importance=data['importance'], retention=RetentionLevel(data['retention']),
        people=tuple(data['people']),
        evidence=tuple(
            EvidenceRef(item['reference'], MemorySource(item['source']),
                        _datetime(item['observed_at'], 'observed_at') if item['observed_at'] else None)
            for item in data['evidence']
        ),
        media=tuple(
            MediaRef(item['media_id'], item['media_type'], item['content_hash'], item['metadata'])
            for item in data['media']
        ),
        conflict_key=data['conflict_key'],
    )


def _encode_conflict(conflict: ConflictCase) -> str:
    return json.dumps(
        {'id': conflict.id, 'key': conflict.key, 'current': json.loads(_encode(conflict.current)),
         'incoming': _candidate_data(conflict.incoming), 'status': conflict.status, 'reason': conflict.reason,
         'current_hash': conflict.current_hash},
        ensure_ascii=False, separators=(',', ':'),
    )


def _decode_conflict(payload: str) -> ConflictCase:
    data = json.loads(payload)
    return ConflictCase(
        data['id'], data['key'], _decode(json.dumps(data['current'])),
        _candidate_from_data(data['incoming']), data['status'], data['reason'], data.get('current_hash', ''),
    )


class MemoryRepository:
    """Unified memory entry point; updating a key replaces its current value."""

    def __init__(self, backend: MemoryBackend, calibrator: ConfidenceCalibrator | None = None) -> None:
        self._backend = backend
        self._calibrator = calibrator or IdentityCalibrator()
        self._lock = RLock()

    @classmethod
    def in_memory(cls) -> 'MemoryRepository':
        return cls(InMemoryBackend())

    @classmethod
    def sqlite(cls, connection: sqlite3.Connection) -> 'MemoryRepository':
        """Inject a caller-owned connection; its thread-safety remains the caller's responsibility."""
        return cls(SQLiteBackend(connection, manage_connection=False))

    @classmethod
    def sqlite_path(cls, path: str | Path) -> 'MemoryRepository':
        """Create a shared-thread SQLite repository with WAL enabled by SQLiteBackend."""
        connection = sqlite3.connect(str(path), check_same_thread=False)
        return cls(SQLiteBackend(connection, manage_connection=True))

    def close(self) -> None:
        self._backend.close()

    def write(self, candidate: MemoryCandidate) -> MemoryRecord:
        with self._lock:
            return self._backend.apply(candidate, self._calibrator)

    def conflicts(self, *, unresolved_only: bool = True) -> tuple[ConflictCase, ...]:
        return self._backend.conflicts(unresolved_only)

    def resolve_conflict(self, conflict_id: str, use_incoming: bool) -> MemoryRecord:
        if type(use_incoming) is not bool:
            raise ValueError('use_incoming must be a bool')
        return self._backend.resolve_conflict(conflict_id, use_incoming)

    def get(self, key: str) -> MemoryRecord | None:
        return self._backend.get(key)

    def records(self) -> tuple[MemoryRecord, ...]:
        """全量记录快照（服务侧自定义打分检索用；记录数有限，内存过滤足够）。"""
        return self._backend.all()

    def query(self, query: MemoryQuery) -> MemoryPacket:
        needle = query.text.casefold()
        people = set(query.people)
        types = set(query.types)
        matches = [
            record for record in self._backend.all()
            if (not needle or needle in record.content.casefold() or needle in record.key.casefold())
            and (not people or people.intersection(record.people))
            and (not types or record.memory_type in types)
        ]
        matches.sort(key=lambda record: (record.importance, record.confidence, record.updated_at), reverse=True)
        return MemoryPacket(tuple(matches[:query.limit]), query)

    def query_text(self, text: str, *, limit: int = 10) -> MemoryPacket:
        return self.query(MemoryQuery(text=text, limit=limit))

    def mark_used(self, record_id: str, at: datetime | None = None) -> MemoryRecord:
        if at is not None and (
            not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() != timezone.utc.utcoffset(at)
        ):
            raise ValueError('at must be an aware UTC datetime')
        with self._lock:
            return self._backend.mark_used(record_id, at or datetime.now(timezone.utc))
