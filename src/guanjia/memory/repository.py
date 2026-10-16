"""Repository facade with injectable in-memory and SQLite storage backends."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
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
    """Small injectable SQLite backend storing immutable domain records as JSON."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = RLock()
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute(
            'CREATE TABLE IF NOT EXISTS guanjia_memories ('
            'key TEXT PRIMARY KEY, payload TEXT NOT NULL)'
        )
        self._connection.execute(
            'CREATE TABLE IF NOT EXISTS guanjia_memory_conflicts ('
            'id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)'
        )
        self._connection.commit()

    def get(self, key: str) -> MemoryRecord | None:
        with self._lock:
            row = self._connection.execute(
                'SELECT payload FROM guanjia_memories WHERE key = ?', (key,)
            ).fetchone()
        return None if row is None else _decode(row[0])

    def put(self, record: MemoryRecord) -> None:
        with self._lock:
            self._connection.execute(
                'INSERT INTO guanjia_memories(key, payload) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                (record.key, _encode(record)),
            )
            self._connection.commit()

    def all(self) -> tuple[MemoryRecord, ...]:
        with self._lock:
            rows = tuple(self._connection.execute('SELECT payload FROM guanjia_memories'))
        return tuple(_decode(row[0]) for row in rows)

    def add_conflict(self, conflict: ConflictCase) -> None:
        with self._lock:
            self._connection.execute(
                'INSERT OR REPLACE INTO guanjia_memory_conflicts(id, status, payload) VALUES(?, ?, ?)',
                (conflict.id, conflict.status, _encode_conflict(conflict)),
            )
            self._connection.commit()

    def conflicts(self, unresolved_only: bool) -> tuple[ConflictCase, ...]:
        sql = 'SELECT payload FROM guanjia_memory_conflicts' + (' WHERE status = ?' if unresolved_only else '')
        args = ('uncertain',) if unresolved_only else ()
        with self._lock:
            rows = tuple(self._connection.execute(sql, args))
        return tuple(_decode_conflict(row[0]) for row in rows)

    def resolve_conflict(self, conflict_id: str, use_incoming: bool) -> MemoryRecord:
        with self._lock:
            row = self._connection.execute(
                'SELECT payload FROM guanjia_memory_conflicts WHERE id = ?', (conflict_id,)
            ).fetchone()
            if row is None:
                raise KeyError(conflict_id)
            conflict = _decode_conflict(row[0])
            record = conflict.current.replace_with(conflict.incoming) if use_incoming else conflict.current
            try:
                self._connection.execute('BEGIN')
                self._connection.execute(
                    'INSERT INTO guanjia_memories(key, payload) VALUES(?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                    (record.key, _encode(record)),
                )
                self._connection.execute('DELETE FROM guanjia_memory_conflicts WHERE id = ?', (conflict_id,))
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return record

    def mark_used(self, record_id: str, at: datetime) -> MemoryRecord:
        with self._lock:
            row = self._connection.execute(
                'SELECT key, payload FROM guanjia_memories'
            ).fetchall()
            for _, payload in row:
                record = _decode(payload)
                if record.id == record_id:
                    used = replace(record, use_count=record.use_count + 1, last_used_at=at)
                    self._connection.execute(
                        'UPDATE guanjia_memories SET payload = ? WHERE key = ?',
                        (_encode(used), used.key),
                    )
                    self._connection.commit()
                    return used
        raise KeyError(record_id)

    def close(self) -> None:
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


def _decode(payload: str) -> MemoryRecord:
    data = json.loads(payload)
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
                datetime.fromisoformat(item['observed_at']) if item['observed_at'] else None,
            )
            for item in data['evidence']
        ),
        media=tuple(
            MediaRef(item['media_id'], item['media_type'], item['content_hash'], item['metadata'])
            for item in data['media']
        ),
        conflict_key=data.get('conflict_key'),
        created_at=datetime.fromisoformat(data['created_at']),
        updated_at=datetime.fromisoformat(data['updated_at']),
        last_used_at=datetime.fromisoformat(data['last_used_at']) if data['last_used_at'] else None,
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
                        datetime.fromisoformat(item['observed_at']) if item['observed_at'] else None)
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
         'incoming': _candidate_data(conflict.incoming), 'status': conflict.status, 'reason': conflict.reason},
        ensure_ascii=False, separators=(',', ':'),
    )


def _decode_conflict(payload: str) -> ConflictCase:
    data = json.loads(payload)
    return ConflictCase(
        data['id'], data['key'], _decode(json.dumps(data['current'])),
        _candidate_from_data(data['incoming']), data['status'], data['reason'],
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
        return cls(SQLiteBackend(connection))

    @classmethod
    def sqlite_path(cls, path: str | Path) -> 'MemoryRepository':
        """Create a shared-thread SQLite repository with WAL enabled by SQLiteBackend."""
        connection = sqlite3.connect(str(path), check_same_thread=False)
        return cls(SQLiteBackend(connection))

    def close(self) -> None:
        self._backend.close()

    def write(self, candidate: MemoryCandidate) -> MemoryRecord:
        with self._lock:
            existing = self._backend.get(candidate.key)
            if existing is None:
                record = MemoryRecord.from_candidate(candidate)
            elif _candidate_data(candidate) == _candidate_data(_as_candidate(existing)):
                return existing
            else:
                decision = decide_update(existing, candidate, self._calibrator)
                record = decision.record
                if decision.conflict is not None:
                    duplicate = any(
                        conflict.key == candidate.key
                        and _candidate_data(conflict.incoming) == _candidate_data(candidate)
                        for conflict in self._backend.conflicts(True)
                    )
                    if not duplicate:
                        self._backend.add_conflict(decision.conflict)
            self._backend.put(record)
            return record

    def conflicts(self, *, unresolved_only: bool = True) -> tuple[ConflictCase, ...]:
        return self._backend.conflicts(unresolved_only)

    def resolve_conflict(self, conflict_id: str, use_incoming: bool) -> MemoryRecord:
        return self._backend.resolve_conflict(conflict_id, use_incoming)

    def get(self, key: str) -> MemoryRecord | None:
        return self._backend.get(key)

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
        with self._lock:
            return self._backend.mark_used(record_id, at or datetime.now(timezone.utc))
