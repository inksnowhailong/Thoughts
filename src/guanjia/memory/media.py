"""Memory-side media reference bookkeeping; media bytes remain external."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import MappingProxyType
from typing import Mapping, Protocol

from .contracts import MediaRef, MemoryRecord
from .repository import _decode, _encode


class MemoryMediaPort(Protocol):
    """The input/media module owns a durable, generation-fenced reference state."""

    def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool: ...
    def get_metadata(self, media_id: str) -> Mapping[str, str] | None: ...


class SQLiteFencedMediaPortAdapter:
    """Reference implementation for media stores that need durable generation fencing."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute(
            'CREATE TABLE IF NOT EXISTS guanjia_fenced_media_references ('
            'stable_ref TEXT PRIMARY KEY, media_id TEXT NOT NULL, retained INTEGER NOT NULL, generation INTEGER NOT NULL)'
        )
        self._connection.commit()
        self._lock = RLock()

    def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool:
        if type(generation) is not int or generation < 1:
            raise ValueError('generation must be a positive integer')
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                row = self._connection.execute(
                    'SELECT generation FROM guanjia_fenced_media_references WHERE stable_ref = ?', (stable_ref,)
                ).fetchone()
                if row is not None and row[0] > generation:
                    self._connection.rollback()
                    return False
                self._connection.execute(
                    'INSERT INTO guanjia_fenced_media_references(stable_ref, media_id, retained, generation) VALUES(?, ?, ?, ?) '
                    'ON CONFLICT(stable_ref) DO UPDATE SET media_id=excluded.media_id, retained=excluded.retained, '
                    'generation=excluded.generation',
                    (stable_ref, media_id, int(retained), generation),
                )
                self._connection.commit()
                return True
            except BaseException:
                self._connection.rollback()
                raise

    def get_metadata(self, media_id: str) -> Mapping[str, str] | None:
        return None

    def close(self) -> None:
        self._connection.close()


@dataclass(frozen=True)
class MemoryMediaRetention:
    """Persistent retention reasons for one memory-to-media relationship."""

    record_key: str
    media_id: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.record_key, str) or not self.record_key.strip():
            raise ValueError('record_key must be a non-empty string')
        if not isinstance(self.media_id, str) or not self.media_id.strip():
            raise ValueError('media_id must be a non-empty string')
        reasons = tuple(dict.fromkeys(_reason(reason) for reason in self.reasons))
        if not reasons:
            raise ValueError('reasons must not be empty')
        object.__setattr__(self, 'reasons', reasons)


@dataclass(frozen=True)
class MemoryMediaState:
    """One atomically stored memory snapshot and its media retention reasons."""

    record: MemoryRecord
    retention: MemoryMediaRetention


@dataclass(frozen=True)
class MemoryMediaOperation:
    op_id: str
    stable_ref: str
    media_id: str
    action: str
    generation: int
    fencing: int


class MemoryMediaStateStore(Protocol):
    """Atomic domain-state and outbox boundary."""

    def get(self, record_key: str, media_id: str) -> MemoryMediaState | None: ...
    def save(self, record: MemoryRecord, retention: MemoryMediaRetention | None) -> None: ...
    def update_reason(
        self,
        record: MemoryRecord,
        media_id: str,
        media_type: str,
        content_hash: str,
        metadata: Mapping[str, str],
        reason: str,
        *,
        retain: bool,
    ) -> tuple[MemoryRecord, MemoryMediaRetention | None]: ...
    def claim(self, *, limit: int, owner: str, now: datetime) -> tuple[MemoryMediaOperation, ...]: ...
    def complete_operation(self, operation: MemoryMediaOperation, *, owner: str) -> bool: ...


class InMemoryMediaStateStore:
    """Thread-safe test implementation; it is intentionally process-local."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], MemoryMediaState] = {}
        self._operations: dict[str, MemoryMediaOperation] = {}
        self._generations: dict[str, int] = {}
        self._claims: dict[str, tuple[str, datetime]] = {}
        self._lock = RLock()

    def get(self, record_key: str, media_id: str) -> MemoryMediaState | None:
        with self._lock:
            return self._states.get((record_key, media_id))

    def save(self, record: MemoryRecord, retention: MemoryMediaRetention | None) -> None:
        with self._lock:
            if retention is None:
                stale_keys = [key for key in self._states if key[0] == record.key]
                for key in stale_keys:
                    if key[1] not in {item.media_id for item in record.media}:
                        del self._states[key]
                for key, state in tuple(self._states.items()):
                    if key[0] == record.key:
                        self._states[key] = MemoryMediaState(record, state.retention)
                return
            if retention.record_key != record.key:
                raise ValueError('retention record_key must match record key')
            for key, state in tuple(self._states.items()):
                if key[0] == record.key:
                    self._states[key] = MemoryMediaState(record, state.retention)
            self._states[(record.key, retention.media_id)] = MemoryMediaState(record, retention)

    def update_reason(
        self,
        record: MemoryRecord,
        media_id: str,
        media_type: str,
        content_hash: str,
        metadata: Mapping[str, str],
        reason: str,
        *,
        retain: bool,
    ) -> tuple[MemoryRecord, MemoryMediaRetention | None]:
        with self._lock:
            current = self._states.get((record.key, media_id))
            current_record = current.record if current else record
            current_reasons = current.retention.reasons if current else ()
            existing = next((item for item in current_record.media if item.media_id == media_id), None)
            if existing is not None and (existing.media_type != media_type or existing.content_hash != content_hash):
                raise ValueError('media reference conflicts with existing record media')
            reasons = (
                current_reasons if reason in current_reasons else current_reasons + (reason,)
            ) if retain else tuple(item for item in current_reasons if item != reason)
            if reasons == current_reasons:
                return current_record, current.retention if current else None
            stable_ref = _stable_ref(record.key, media_id)
            if reasons:
                ref = existing or MediaRef(media_id, media_type, content_hash, metadata)
                updated = current_record if existing else replace(current_record, media=current_record.media + (ref,))
                retention = MemoryMediaRetention(record.key, media_id, reasons)
                self.save(updated, retention)
                if retain and not current_reasons:
                    generation = self._generations.get(stable_ref, 0) + 1
                    self._generations[stable_ref] = generation
                    self._operations[stable_ref] = MemoryMediaOperation(
                        stable_ref, stable_ref, media_id, 'retain', generation, 0,
                    )
                return updated, retention
            updated = replace(current_record, media=tuple(item for item in current_record.media if item.media_id != media_id))
            self.save(updated, None)
            generation = self._generations.get(stable_ref, 0) + 1
            self._generations[stable_ref] = generation
            self._operations[stable_ref] = MemoryMediaOperation(
                stable_ref, stable_ref, media_id, 'release', generation, 0,
            )
            return updated, None

    def claim(self, *, limit: int, owner: str, now: datetime) -> tuple[MemoryMediaOperation, ...]:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError('now must be an aware UTC datetime')
        with self._lock:
            claimed: list[MemoryMediaOperation] = []
            for operation in self._operations.values():
                current = self._claims.get(operation.op_id)
                if current is None or current[1] <= now:
                    self._claims[operation.op_id] = (owner, now + timedelta(minutes=5))
                    claimed.append(operation)
                    if len(claimed) == limit:
                        break
            return tuple(claimed)

    def complete_operation(self, operation: MemoryMediaOperation, *, owner: str) -> bool:
        with self._lock:
            if self._operations.get(operation.stable_ref) == operation and self._claims.get(operation.op_id, ('', None))[0] == owner:
                del self._operations[operation.stable_ref]
                self._claims.pop(operation.op_id, None)
                return True
            return False


class SQLiteMemoryMediaStateStore:
    """SQLite-backed media state and durable outbox for restart-safe reconciliation."""

    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.execute('PRAGMA foreign_keys=ON')
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute('PRAGMA busy_timeout=5000')
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            legacy_columns = tuple(
                row[1] for row in self._connection.execute('PRAGMA table_info(guanjia_memory_media_states)')
            )
            if legacy_columns and 'record_payload' in legacy_columns:
                self._connection.execute('ALTER TABLE guanjia_memory_media_states RENAME TO guanjia_memory_media_states_legacy')
            self._connection.execute(
                'CREATE TABLE IF NOT EXISTS guanjia_memory_media_records ('
                'record_key TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL)'
            )
            self._connection.execute(
                'CREATE TABLE IF NOT EXISTS guanjia_memory_media_states ('
                'record_key TEXT NOT NULL, media_id TEXT NOT NULL, reasons_payload TEXT NOT NULL, '
                'PRIMARY KEY(record_key, media_id), '
                'FOREIGN KEY(record_key) REFERENCES guanjia_memory_media_records(record_key))'
            )
            self._connection.execute(
                'CREATE TABLE IF NOT EXISTS guanjia_memory_media_outbox_v2 ('
                'op_id TEXT PRIMARY KEY, stable_ref TEXT UNIQUE NOT NULL, media_id TEXT NOT NULL, action TEXT NOT NULL, '
                'generation INTEGER NOT NULL, status TEXT NOT NULL, claim_owner TEXT, lease_until TEXT, fencing INTEGER NOT NULL)'
            )
            if legacy_columns and 'record_payload' in legacy_columns:
                self._migrate_legacy()
                self._connection.execute('DROP TABLE guanjia_memory_media_states_legacy')
                if self._table_exists('guanjia_memory_media_outbox'):
                    self._connection.execute('DROP TABLE guanjia_memory_media_outbox')
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            self._connection.close()
            raise
        self._lock = RLock()

    def _table_exists(self, name: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    def _migrate_legacy(self) -> None:
        """Migrate legacy rows exactly once, choosing only the highest revision per record."""
        rows = tuple(self._connection.execute(
            'SELECT record_key, media_id, record_payload, reasons_payload, revision '
            'FROM guanjia_memory_media_states_legacy'
        ))
        latest: dict[str, tuple[str, int]] = {}
        relations: dict[tuple[str, str], tuple[str, ...]] = {}
        for record_key, media_id, payload, reasons_payload, revision in rows:
            prior = latest.get(record_key)
            if prior is None or revision > prior[1]:
                latest[record_key] = (payload, revision)
            elif revision == prior[1] and payload != prior[0]:
                raise ValueError('legacy media record revisions conflict')
            try:
                reasons = tuple(json.loads(reasons_payload))
            except (TypeError, ValueError) as error:
                raise ValueError('invalid legacy media reasons') from error
            relation_key = (record_key, media_id)
            relations[relation_key] = tuple(dict.fromkeys(relations.get(relation_key, ()) + reasons))
        for record_key, (payload, revision) in latest.items():
            self._connection.execute(
                'INSERT INTO guanjia_memory_media_records(record_key, payload, revision) VALUES(?, ?, ?)',
                (record_key, payload, revision),
            )
        for (record_key, media_id), reasons in relations.items():
            self._connection.execute(
                'INSERT INTO guanjia_memory_media_states(record_key, media_id, reasons_payload) VALUES(?, ?, ?)',
                (record_key, media_id, json.dumps(reasons)),
            )
        if self._table_exists('guanjia_memory_media_outbox'):
            outbox: dict[str, tuple[str, str, str]] = {}
            for stable_ref, media_id, action, status in self._connection.execute(
                'SELECT stable_ref, media_id, action, status FROM guanjia_memory_media_outbox'
            ):
                prior = outbox.get(stable_ref)
                if prior is not None and (prior[0] != media_id or prior[1] != action):
                    raise ValueError('legacy media outbox conflicts')
                outbox[stable_ref] = (
                    media_id, action, 'pending' if status == 'pending' or (prior and prior[2] == 'pending') else 'done',
                )
            for stable_ref, (media_id, action, status) in outbox.items():
                self._connection.execute(
                    'INSERT INTO guanjia_memory_media_outbox_v2'
                    '(op_id, stable_ref, media_id, action, generation, status, claim_owner, lease_until, fencing) '
                    'VALUES(?, ?, ?, ?, 1, ?, NULL, NULL, 0)',
                    (stable_ref, stable_ref, media_id, action, status),
                )

    def get(self, record_key: str, media_id: str) -> MemoryMediaState | None:
        with self._lock:
            row = self._connection.execute(
                'SELECT records.payload, states.reasons_payload FROM guanjia_memory_media_states AS states '
                'JOIN guanjia_memory_media_records AS records ON records.record_key = states.record_key '
                'WHERE states.record_key = ? AND states.media_id = ?', (record_key, media_id)
            ).fetchone()
        if row is None:
            return None
        return MemoryMediaState(
            _decode(row[0]),
            MemoryMediaRetention(record_key, media_id, tuple(json.loads(row[1]))),
        )

    def save(self, record: MemoryRecord, retention: MemoryMediaRetention | None) -> None:
        """Compatibility API used by existing callers; write state transactionally."""
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                self._connection.execute(
                    'INSERT INTO guanjia_memory_media_records(record_key, payload, revision) VALUES(?, ?, 1) '
                    'ON CONFLICT(record_key) DO UPDATE SET payload=excluded.payload, revision=revision+1',
                    (record.key, _encode(record)),
                )
                if retention is None:
                    retained_ids = {item.media_id for item in record.media}
                    for (media_id,) in self._connection.execute(
                        'SELECT media_id FROM guanjia_memory_media_states WHERE record_key = ?', (record.key,)
                    ):
                        if media_id not in retained_ids:
                            self._connection.execute(
                                'DELETE FROM guanjia_memory_media_states WHERE record_key = ? AND media_id = ?',
                                (record.key, media_id),
                            )
                else:
                    self._connection.execute(
                        'INSERT INTO guanjia_memory_media_states(record_key, media_id, reasons_payload) VALUES(?, ?, ?) '
                        'ON CONFLICT(record_key, media_id) DO UPDATE SET reasons_payload=excluded.reasons_payload',
                        (record.key, retention.media_id, json.dumps(retention.reasons)),
                    )
                self._connection.commit()
            except BaseException:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
                raise

    def update_reason(
        self,
        record: MemoryRecord,
        media_id: str,
        media_type: str,
        content_hash: str,
        metadata: Mapping[str, str],
        reason: str,
        *,
        retain: bool,
    ) -> tuple[MemoryRecord, MemoryMediaRetention | None]:
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                row = self._connection.execute(
                    'SELECT records.payload, records.revision, states.reasons_payload FROM guanjia_memory_media_records AS records '
                    'LEFT JOIN guanjia_memory_media_states AS states '
                    'ON states.record_key = records.record_key AND states.media_id = ? '
                    'WHERE records.record_key = ?', (media_id, record.key)
                ).fetchone()
                current_record = _decode(row[0]) if row is not None else record
                current_reasons = tuple(json.loads(row[2])) if row is not None and row[2] is not None else ()
                state = (
                    MemoryMediaState(current_record, MemoryMediaRetention(record.key, media_id, current_reasons))
                    if current_reasons else None
                )
                existing = next((item for item in current_record.media if item.media_id == media_id), None)
                if existing is not None and (existing.media_type != media_type or existing.content_hash != content_hash):
                    raise ValueError('media reference conflicts with existing record media')
                reasons = (
                    current_reasons if reason in current_reasons else current_reasons + (reason,)
                ) if retain else tuple(item for item in current_reasons if item != reason)
                if reasons == current_reasons:
                    self._connection.commit()
                    return current_record, state.retention if state else None
                stable_ref = _stable_ref(record.key, media_id)
                if reasons:
                    ref = existing or MediaRef(media_id, media_type, content_hash, metadata)
                    updated = current_record if existing else replace(current_record, media=current_record.media + (ref,))
                    retention = MemoryMediaRetention(record.key, media_id, reasons)
                    if row is None:
                        self._connection.execute(
                            'INSERT INTO guanjia_memory_media_records(record_key, payload, revision) VALUES(?, ?, 1)',
                            (record.key, _encode(updated)),
                        )
                    else:
                        changed = self._connection.execute(
                            'UPDATE guanjia_memory_media_records SET payload = ?, revision = revision + 1 '
                            'WHERE record_key = ? AND revision = ?',
                            (_encode(updated), record.key, row[1]),
                        )
                        if changed.rowcount != 1:
                            raise RuntimeError('media state changed during compare-and-set')
                    self._connection.execute(
                        'INSERT INTO guanjia_memory_media_states(record_key, media_id, reasons_payload) VALUES(?, ?, ?) '
                        'ON CONFLICT(record_key, media_id) DO UPDATE SET reasons_payload=excluded.reasons_payload',
                        (record.key, media_id, json.dumps(reasons)),
                    )
                    if retain and not current_reasons:
                        self._queue(stable_ref, media_id, 'retain')
                    self._connection.commit()
                    return updated, retention
                updated = replace(current_record, media=tuple(item for item in current_record.media if item.media_id != media_id))
                self._connection.execute(
                    'DELETE FROM guanjia_memory_media_states WHERE record_key = ? AND media_id = ?',
                    (record.key, media_id),
                )
                changed = self._connection.execute(
                    'UPDATE guanjia_memory_media_records SET payload = ?, revision = revision + 1 '
                    'WHERE record_key = ? AND revision = ?',
                    (_encode(updated), record.key, row[1]),
                )
                if changed.rowcount != 1:
                    raise RuntimeError('media state changed during compare-and-set')
                self._queue(stable_ref, media_id, 'release')
                self._connection.commit()
                return updated, None
            except BaseException:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _queue(self, stable_ref: str, media_id: str, action: str) -> None:
        self._connection.execute(
            'INSERT INTO guanjia_memory_media_outbox_v2'
            '(op_id, stable_ref, media_id, action, generation, status, claim_owner, lease_until, fencing) '
            'VALUES(?, ?, ?, ?, 1, "pending", NULL, NULL, 0) '
            'ON CONFLICT(stable_ref) DO UPDATE SET media_id=excluded.media_id, action=excluded.action, '
            'generation=generation+1, status="pending", claim_owner=NULL, lease_until=NULL, fencing=fencing+1',
            (stable_ref, stable_ref, media_id, action),
        )

    def claim(self, *, limit: int, owner: str, now: datetime) -> tuple[MemoryMediaOperation, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be a positive integer')
        if not isinstance(owner, str) or not owner:
            raise ValueError('owner must be a non-empty string')
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError('now must be an aware UTC datetime')
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            try:
                lease_until = (now + timedelta(minutes=5)).isoformat()
                rows = tuple(self._connection.execute(
                    'SELECT op_id, stable_ref, media_id, action, generation, fencing FROM guanjia_memory_media_outbox_v2 '
                    'WHERE status = "pending" OR (status = "claimed" AND lease_until <= ?) '
                    'ORDER BY op_id LIMIT ?', (now.isoformat(), limit),
                ))
                claimed: list[MemoryMediaOperation] = []
                for row in rows:
                    changed = self._connection.execute(
                        'UPDATE guanjia_memory_media_outbox_v2 SET status="claimed", claim_owner=?, lease_until=?, '
                        'fencing=fencing+1 WHERE op_id=? AND generation=? AND '
                        '(status="pending" OR (status="claimed" AND lease_until <= ?))',
                        (owner, lease_until, row[0], row[4], now.isoformat()),
                    )
                    if changed.rowcount:
                        claimed.append(MemoryMediaOperation(*row[:5], row[5] + 1))
                self._connection.commit()
                return tuple(claimed)
            except BaseException:
                self._connection.rollback()
                raise

    def complete_operation(self, operation: MemoryMediaOperation, *, owner: str) -> bool:
        with self._lock:
            changed = self._connection.execute(
                'UPDATE guanjia_memory_media_outbox_v2 SET status = "done" '
                'WHERE op_id = ? AND generation = ? AND fencing = ? AND claim_owner = ? AND status = "claimed"',
                (operation.op_id, operation.generation, operation.fencing, owner),
            )
            self._connection.commit()
            return changed.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _reason(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('retention reason must be a non-empty string')
    return value


@dataclass(frozen=True)
class MediaReferenceUpdate:
    """A record replacement and its immutable retention-reason view."""

    record: MemoryRecord
    reasons: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.record, MemoryRecord):
            raise ValueError('record must be a MemoryRecord')
        object.__setattr__(
            self,
            'reasons',
            MappingProxyType({media_id: tuple(values) for media_id, values in self.reasons.items()}),
        )


class MemoryMediaService:
    """Maintain only a MemoryRecord's media references and retention reasons."""

    def __init__(self, port: MemoryMediaPort, state_store: MemoryMediaStateStore) -> None:
        self._port = port
        self._state_store = state_store

    def retain(
        self,
        record: MemoryRecord,
        media_id: str,
        media_type: str,
        content_hash: str,
        reason: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> MediaReferenceUpdate:
        reason = _reason(reason)
        updated_record, retention = self._state_store.update_reason(
            record, media_id, media_type, content_hash, metadata or {}, reason, retain=True,
        )
        return self._update(updated_record, retention)

    def release(
        self,
        record: MemoryRecord,
        media_id: str,
        reason: str,
    ) -> MediaReferenceUpdate:
        reason = _reason(reason)
        state = self._state_store.get(record.key, media_id)
        if state is None:
            return self._update(record, None)
        media = next(item for item in state.record.media if item.media_id == media_id)
        updated_record, retention = self._state_store.update_reason(
            state.record, media_id, media.media_type, media.content_hash, media.metadata, reason, retain=False,
        )
        return self._update(updated_record, retention)

    def reconcile(self, *, owner: str = 'memory-media-reconciler', now: datetime | None = None) -> None:
        """Claim and deliver durable operations; only the fenced claimant may complete."""
        claimed_at = now or datetime.now(timezone.utc)
        for operation in self._state_store.claim(limit=100, owner=owner, now=claimed_at):
            applied = self._port.apply_reference(
                operation.media_id,
                operation.stable_ref,
                operation.action == 'retain',
                operation.generation,
            )
            if applied:
                self._state_store.complete_operation(operation, owner=owner)

    def metadata(self, media_id: str) -> Mapping[str, str] | None:
        """Expose port metadata without reading a repository or media database."""
        return self._port.get_metadata(media_id)

    @staticmethod
    def _validate_reference(
        record: MemoryRecord,
        media_id: str,
        media_type: str,
        content_hash: str,
    ) -> None:
        existing = next((item for item in record.media if item.media_id == media_id), None)
        if existing is not None and (existing.media_type != media_type or existing.content_hash != content_hash):
            raise ValueError('media reference conflicts with existing record media')

    @staticmethod
    def _stable_ref(record_key: str, media_id: str) -> str:
        return _stable_ref(record_key, media_id)

    @staticmethod
    def _update(
        record: MemoryRecord,
        retention: MemoryMediaRetention | None,
    ) -> MediaReferenceUpdate:
        reasons = {} if retention is None else {retention.media_id: retention.reasons}
        return MediaReferenceUpdate(record, reasons)


def _stable_ref(record_key: str, media_id: str) -> str:
    return f'memory:{record_key}:{media_id}'
