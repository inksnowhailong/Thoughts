"""Content-addressed media storage with controlled reads and retention pruning."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Protocol

from guanjia.contracts import JsonValue

from .contracts import MediaRef, _frozen_mapping


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('timestamp must be an aware UTC datetime')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


class MediaStore:
    """Stores bytes by SHA-256 and never changes filesystem ACLs."""

    def __init__(
        self,
        root: str | Path,
        authorizer: 'MediaReadAuthorizer',
        database_path: str | Path | None = None,
        *,
        operation_lease_seconds: float = 30,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not callable(getattr(authorizer, 'authorize', None)):
            raise TypeError('authorizer must implement MediaReadAuthorizer')
        if (
            not isinstance(operation_lease_seconds, (int, float))
            or isinstance(operation_lease_seconds, bool)
            or not math.isfinite(operation_lease_seconds)
            or operation_lease_seconds <= 0
        ):
            raise ValueError('operation_lease_seconds must be a positive finite number')
        self.root = Path(root)
        self._authorizer = authorizer
        self._owner = uuid.uuid4().hex
        self._lease_seconds = operation_lease_seconds
        self._fault_injector = fault_injector
        self.objects = self.root / 'objects'
        self.quarantine = self.root / 'quarantine'
        self.objects.mkdir(parents=True, exist_ok=True)
        self.quarantine.mkdir(parents=True, exist_ok=True)
        database = Path(database_path) if database_path else self.root / 'media.sqlite3'
        database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute('PRAGMA busy_timeout=5000')
        self._connection.execute('PRAGMA foreign_keys=ON')
        self._connection.execute(
            '''CREATE TABLE IF NOT EXISTS media (
                sha256 TEXT PRIMARY KEY,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL
            )'''
        )
        self._connection.execute(
            '''CREATE TABLE IF NOT EXISTS media_references (
                sha256 TEXT NOT NULL REFERENCES media(sha256) ON DELETE CASCADE,
                reference_id TEXT NOT NULL,
                PRIMARY KEY (sha256, reference_id)
            )'''
        )
        self._connection.execute(
            '''CREATE TABLE IF NOT EXISTS media_operations (
                op_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('ingest', 'prune')),
                sha256 TEXT NOT NULL,
                path TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('prepared', 'moved', 'db_committed')),
                owner TEXT NOT NULL,
                lease_until TEXT NOT NULL,
                created_at TEXT NOT NULL
            )'''
        )
        self._connection.commit()
        self.reconcile()

    def _fault(self, phase: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(phase)

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def reconcile(self, *, now: datetime | None = None) -> None:
        """Recover only expired, journaled operations; unknown objects are untouched."""
        current = now or datetime.now(timezone.utc)
        now_text = _utc_text(current)
        with self._lock:
            rows = self._connection.execute(
                'SELECT * FROM media_operations WHERE lease_until <= ?', (now_text,)
            ).fetchall()
        for row in rows:
            with self._lock:
                claimed = self._connection.execute(
                    '''UPDATE media_operations SET owner = ?, lease_until = ?
                       WHERE op_id = ? AND lease_until <= ?''',
                    (self._owner, _utc_text(current + timedelta(seconds=self._lease_seconds)),
                     row['op_id'], now_text),
                )
                self._connection.commit()
            if claimed.rowcount != 1:
                continue
            if row['kind'] == 'ingest':
                self._resume_ingest(row['op_id'])
            else:
                self._resume_prune(row['op_id'])

    def list_orphans(self, *, minimum_age_seconds: float = 0) -> tuple[Path, ...]:
        """Report unknown CAS files; maintenance must explicitly decide removal."""
        if (
            not isinstance(minimum_age_seconds, (int, float))
            or isinstance(minimum_age_seconds, bool)
            or not math.isfinite(minimum_age_seconds)
            or minimum_age_seconds < 0
        ):
            raise ValueError('minimum_age_seconds must be a non-negative finite number')
        threshold = datetime.now(timezone.utc).timestamp() - minimum_age_seconds
        with self._lock:
            known = {
                row['sha256'] for row in self._connection.execute('SELECT sha256 FROM media')
            }
            return tuple(
                path for folder in self.objects.glob('*') if folder.is_dir()
                for path in folder.iterdir()
                if path.is_file() and path.name not in known and path.stat().st_mtime <= threshold
            )

    def list_orphan_temps(self, *, minimum_age_seconds: float = 0) -> tuple[Path, ...]:
        """Report untracked temporary files without deleting them."""
        if (
            not isinstance(minimum_age_seconds, (int, float))
            or isinstance(minimum_age_seconds, bool)
            or minimum_age_seconds < 0
            or not math.isfinite(minimum_age_seconds)
        ):
            raise ValueError('minimum_age_seconds must be a non-negative finite number')
        threshold = datetime.now(timezone.utc).timestamp() - minimum_age_seconds
        with self._lock:
            tracked = {row['path'] for row in self._connection.execute('SELECT path FROM media_operations')}
            return tuple(
                path for path in self.quarantine.glob('*.part')
                if str(path) not in tracked
                and (minimum_age_seconds == 0 or path.stat().st_mtime <= threshold)
            )

    def cleanup_orphan_temps(self, *, minimum_age_seconds: float, acknowledge: bool) -> tuple[Path, ...]:
        if not acknowledge:
            raise PermissionError('cleanup_orphan_temps requires acknowledge=True')
        paths = self.list_orphan_temps(minimum_age_seconds=minimum_age_seconds)
        for path in paths:
            path.unlink(missing_ok=True)
        return paths

    def _object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def ingest_stream(
        self,
        stream: BinaryIO,
        media_type: str,
        *,
        metadata: Mapping[str, JsonValue] | None = None,
        expires_at: datetime | None = None,
        reference_id: str | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> MediaRef:
        """Hash and write a stream once, then atomically move it into the CAS."""
        if not isinstance(media_type, str) or not media_type.strip():
            raise ValueError('media_type must be a non-empty string')
        if reference_id is not None and (not isinstance(reference_id, str) or not reference_id.strip()):
            raise ValueError('reference_id must be a non-empty string')
        if expires_at is not None:
            _utc_text(expires_at)
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError('metadata must be a JSON object')
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
            raise ValueError('chunk_size must be a positive integer')
        frozen_metadata = _frozen_mapping(metadata)
        import json

        metadata_json = json.dumps(frozen_metadata, default=lambda value: dict(value.items()), sort_keys=True)
        temporary = self.quarantine / f'{uuid.uuid4().hex}.part'
        digest = hashlib.sha256()
        size = 0
        with temporary.open('xb') as target:
            while chunk := stream.read(chunk_size):
                if not isinstance(chunk, bytes):
                    raise TypeError('media stream must yield bytes')
                digest.update(chunk)
                target.write(chunk)
                size += len(chunk)
        sha256 = digest.hexdigest()
        op_id = uuid.uuid4().hex
        payload = json.dumps(
            {
                'media_type': media_type, 'size_bytes': size, 'metadata_json': metadata_json,
                'expires_at': _utc_text(expires_at) if expires_at else None,
                'reference_id': reference_id,
            },
            sort_keys=True,
        )
        current = datetime.now(timezone.utc)
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            self._connection.execute(
                '''INSERT INTO media_operations(op_id, kind, sha256, path, payload_json, phase, owner, lease_until, created_at)
                   VALUES (?, 'ingest', ?, ?, ?, 'prepared', ?, ?, ?)''',
                (op_id, sha256, str(temporary), payload, self._owner,
                 _utc_text(current + timedelta(seconds=self._lease_seconds)), _utc_text(current)),
            )
            self._connection.commit()
        self._fault('ingest_prepared')
        self._resume_ingest(op_id)
        return self.get_ref(sha256)

    def ingest_bytes(self, content: bytes, media_type: str, **kwargs: object) -> MediaRef:
        from io import BytesIO

        return self.ingest_stream(BytesIO(content), media_type, **kwargs)

    def _operation(self, op_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            'SELECT * FROM media_operations WHERE op_id = ? AND owner = ?', (op_id, self._owner)
        ).fetchone()

    def _resume_ingest(self, op_id: str) -> None:
        import json

        with self._lock:
            row = self._operation(op_id)
            if row is None:
                return
            payload = json.loads(row['payload_json'])
            temporary = Path(row['path'])
            destination = self._object_path(row['sha256'])
            if row['phase'] == 'prepared':
                with self._transaction():
                    row = self._operation(op_id)
                    if row is None or row['phase'] != 'prepared':
                        return
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if temporary.exists() and not destination.exists():
                        os.replace(temporary, destination)
                    elif temporary.exists():
                        temporary.unlink()
                    changed = self._connection.execute(
                        'UPDATE media_operations SET phase = ? WHERE op_id = ? AND owner = ?',
                        ('moved', op_id, self._owner),
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError('lost ingest operation fence')
                self._fault('ingest_moved')
                row = self._operation(op_id)
                if row is None:
                    return
            if row['phase'] == 'moved':
                self._connection.execute('BEGIN IMMEDIATE')
                existing = self._connection.execute(
                    'SELECT * FROM media WHERE sha256 = ?', (row['sha256'],)
                ).fetchone()
                if existing is None:
                    self._connection.execute(
                        '''INSERT INTO media(sha256, media_type, size_bytes, metadata_json, expires_at, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)''',
                        (row['sha256'], payload['media_type'], payload['size_bytes'],
                         payload['metadata_json'], payload['expires_at'], _utc_text(datetime.now(timezone.utc))),
                    )
                elif (
                    existing['media_type'] != payload['media_type']
                    or existing['size_bytes'] != payload['size_bytes']
                    or existing['metadata_json'] != payload['metadata_json']
                ):
                    self._connection.rollback()
                    raise ValueError('CAS hash conflicts with stored media facts')
                elif payload['expires_at'] and (
                    existing['expires_at'] is None or payload['expires_at'] > existing['expires_at']
                ):
                    self._connection.execute(
                        'UPDATE media SET expires_at = ? WHERE sha256 = ?',
                        (payload['expires_at'], row['sha256']),
                    )
                if payload['reference_id']:
                    self._retain_locked(row['sha256'], payload['reference_id'])
                changed = self._connection.execute(
                    '''UPDATE media_operations SET phase = 'db_committed'
                       WHERE op_id = ? AND owner = ?''', (op_id, self._owner)
                )
                if changed.rowcount != 1:
                    self._connection.rollback()
                    return
                self._connection.commit()
                self._fault('ingest_db_committed')
            self._connection.execute(
                'DELETE FROM media_operations WHERE op_id = ? AND owner = ?', (op_id, self._owner)
            )
            self._connection.commit()

    def _resume_prune(self, op_id: str) -> None:
        import json

        with self._lock:
            row = self._operation(op_id)
            if row is None:
                return
            tombstone = Path(row['path'])
            source = self._object_path(row['sha256'])
            if row['phase'] == 'prepared':
                with self._transaction():
                    row = self._operation(op_id)
                    if row is None or row['phase'] != 'prepared':
                        return
                    if source.exists() and not tombstone.exists():
                        tombstone.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(source, tombstone)
                    changed = self._connection.execute(
                        'UPDATE media_operations SET phase = ? WHERE op_id = ? AND owner = ?',
                        ('moved', op_id, self._owner),
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError('lost prune operation fence')
                self._fault('prune_moved')
                row = self._operation(op_id)
                if row is None:
                    return
            if row['phase'] == 'moved':
                self._connection.execute('BEGIN IMMEDIATE')
                referenced = self._connection.execute(
                    'SELECT 1 FROM media_references WHERE sha256 = ?', (row['sha256'],)
                ).fetchone()
                if referenced is not None:
                    if tombstone.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(tombstone, source)
                    self._connection.execute('DELETE FROM media_operations WHERE op_id = ?', (op_id,))
                    self._connection.commit()
                    return
                self._connection.execute('DELETE FROM media WHERE sha256 = ?', (row['sha256'],))
                changed = self._connection.execute(
                    '''UPDATE media_operations SET phase = 'db_committed'
                       WHERE op_id = ? AND owner = ?''', (op_id, self._owner)
                )
                if changed.rowcount != 1:
                    self._connection.rollback()
                    return
                self._connection.commit()
                self._fault('prune_db_committed')
            if tombstone.exists():
                tombstone.unlink()
            self._connection.execute(
                'DELETE FROM media_operations WHERE op_id = ? AND owner = ?', (op_id, self._owner)
            )
            self._connection.commit()

    def get_ref(self, sha256: str) -> MediaRef:
        with self._lock:
            row = self._connection.execute('SELECT * FROM media WHERE sha256 = ?', (sha256,)).fetchone()
        if row is None:
            raise KeyError(sha256)
        import json

        expiry = (
            datetime.fromisoformat(row['expires_at'][:-1] + '+00:00')
            if row['expires_at'] is not None
            else None
        )
        return MediaRef(
            media_id=row['sha256'],
            sha256=row['sha256'],
            media_type=_media_category(row['media_type']),
            size_bytes=row['size_bytes'],
            metadata=json.loads(row['metadata_json']),
            expires_at=expiry,
        )

    def retain(self, media_id: str, reference_id: str, *, commit: bool = True) -> None:
        if not reference_id:
            raise ValueError('reference_id must be non-empty')
        with self._transaction():
            self._cancel_pending_prune_locked(media_id)
            self._retain_locked(media_id, reference_id)

    def _cancel_pending_prune_locked(self, media_id: str) -> None:
        """Make a tombstoned object readable before adding its new reference."""
        operations = self._connection.execute(
            '''SELECT * FROM media_operations
               WHERE kind = 'prune' AND sha256 = ? AND phase IN ('prepared', 'moved')''',
            (media_id,),
        ).fetchall()
        source = self._object_path(media_id)
        for operation in operations:
            tombstone = Path(operation['path'])
            if tombstone.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tombstone, source)
            self._connection.execute(
                'DELETE FROM media_operations WHERE op_id = ?', (operation['op_id'],)
            )

    def _retain_locked(self, media_id: str, reference_id: str) -> None:
        if self._connection.execute(
            'SELECT 1 FROM media WHERE sha256 = ?', (media_id,)
        ).fetchone() is None:
            raise KeyError(media_id)
        self._connection.execute(
            'INSERT OR IGNORE INTO media_references(sha256, reference_id) VALUES (?, ?)',
            (media_id, reference_id),
        )

    def release(self, media_id: str, reference_id: str) -> bool:
        if not isinstance(reference_id, str) or not reference_id.strip():
            raise ValueError('reference_id must be a non-empty string')
        with self._lock:
            cursor = self._connection.execute(
                'DELETE FROM media_references WHERE sha256 = ? AND reference_id = ?',
                (media_id, reference_id),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    def open_media(self, media_id: str, *, access_context: object) -> BinaryIO:
        """Open bytes only for a known database record, never an arbitrary path."""
        if not isinstance(media_id, str) or not media_id.strip():
            raise ValueError('media_id must be a non-empty string')
        media_ref = self.get_ref(media_id)
        if not self._authorizer.authorize(media_ref, access_context):
            raise PermissionError('media read is not authorized')
        with self._transaction():
            self._cancel_pending_prune_locked(media_id)
        path = self._object_path(media_id)
        if not path.is_file():
            raise FileNotFoundError(media_id)
        return path.open('rb')

    def prune(self, *, now: datetime, max_bytes: int) -> tuple[str, ...]:
        """Remove unreferenced expired objects, then oldest unreferenced objects for capacity."""
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
            raise ValueError('max_bytes must be non-negative')
        now_text = _utc_text(now)
        with self._lock:
            self._connection.execute('BEGIN IMMEDIATE')
            rows = self._connection.execute(
                '''SELECT media.* FROM media LEFT JOIN media_references refs ON refs.sha256 = media.sha256
                   GROUP BY media.sha256 HAVING COUNT(refs.reference_id) = 0 ORDER BY media.created_at ASC'''
            ).fetchall()
            total = sum(row['size_bytes'] for row in self._connection.execute('SELECT size_bytes FROM media'))
            removable = [row for row in rows if row['expires_at'] is not None and row['expires_at'] <= now_text]
            total -= sum(row['size_bytes'] for row in removable)
            for row in rows:
                if row not in removable and total > max_bytes:
                    removable.append(row)
                    total -= row['size_bytes']
            operations: list[str] = []
            for row in removable:
                op_id = uuid.uuid4().hex
                tombstone = self.quarantine / f'{op_id}.tombstone'
                self._connection.execute(
                    '''INSERT INTO media_operations(op_id, kind, sha256, path, payload_json, phase, owner, lease_until, created_at)
                       VALUES (?, 'prune', ?, ?, '{}', 'prepared', ?, ?, ?)''',
                    (op_id, row['sha256'], str(tombstone), self._owner,
                     _utc_text(now + timedelta(seconds=self._lease_seconds)), now_text),
                )
                operations.append(op_id)
            self._connection.commit()
        for op_id in operations:
            self._resume_prune(op_id)
        return tuple(
            row['sha256'] for row in removable
            if self._connection.execute('SELECT 1 FROM media WHERE sha256 = ?', (row['sha256'],)).fetchone() is None
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class MediaReadAuthorizer(Protocol):
    """Local access gate for media bytes; it intentionally does not depend on permissions."""

    def authorize(self, media_ref: MediaRef, access_context: object) -> bool:
        """Return whether this read context may access this media reference."""


def _media_category(media_type: str) -> str:
    prefix = media_type.split('/', 1)[0]
    return prefix if prefix in {'audio', 'image', 'video'} else 'attachment'
