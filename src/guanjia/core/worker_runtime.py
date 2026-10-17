"""Durable, idempotent worker-task state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Callable, Mapping, Protocol, Sequence

from .contracts import ArtifactRef, TaskSpec, task_spec_from_dict, task_spec_to_dict


import json
import sqlite3
import math
import uuid
from pathlib import Path
from datetime import UTC, datetime, timedelta
from hashlib import sha256


class WorkerProtocolError(RuntimeError):
    """Adapter or durable-operation protocol violation."""


class StateTransitionError(WorkerProtocolError):
    """The requested action is not legal from the persisted task state."""


class OperationConflict(WorkerProtocolError):
    """An operation id was reused for different action content."""


class WorkerAdapter(Protocol):
    """Only worker-scoped OpenClaw calls are permitted."""

    def session_for(self, logical_session: str) -> str: ...

    def call(
        self, *, logical_session: str, model: str, command: str, operation_id: str,
        messages: Sequence[Mapping[str, object]],
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class Materials:
    """Only explicit TaskSpec artifact references may reach a worker."""

    references: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if type(self.references) is not tuple or not all(isinstance(item, ArtifactRef) for item in self.references):
            raise ValueError('materials references must be a tuple')


@dataclass(frozen=True)
class MemoryPacket:
    """A bounded list of short, pre-selected memory summaries."""

    summaries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.summaries) is not tuple or len(self.summaries) > 8:
            raise ValueError('memory packet must contain at most eight summaries')
        if not all(type(item) is str and 0 < len(item) <= 500 for item in self.summaries):
            raise ValueError('memory summaries must be short strings')


@dataclass(frozen=True)
class WorkerSnapshot:
    task_id: str
    revision: int
    status: str
    session: str
    progress: int = 0


@dataclass
class _PersistentTask:
    spec: TaskSpec
    session: str = ''
    status: str = 'starting'
    progress: int = 0
    pending: str | None = None
    pending_fingerprint: str | None = None
    pending_phase: str | None = None
    pending_response: dict[str, object] | None = None
    previous_status: str | None = None
    claim_owner: str | None = None
    claim_until: datetime | None = None
    operations: dict[str, tuple[str, WorkerSnapshot]] = field(default_factory=dict)

    def snapshot(self) -> WorkerSnapshot:
        return WorkerSnapshot(self.spec.task_id, self.spec.revision, self.status, self.session, self.progress)


class InMemoryWorkerStore:
    """Thread-safe persistence double; retain it to restore state after restart."""

    def __init__(self, *, lease_seconds: float = 30.0, clock: Callable[[], datetime] | None = None) -> None:
        if not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('lease_seconds must be a positive finite number')
        self.lock = RLock()
        self.tasks: dict[str, _PersistentTask] = {}
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError('clock must return an aware UTC datetime')
        return now.astimezone(UTC)

    def reserve_start(self, spec: TaskSpec, operation_id: str, fingerprint: str, owner: str = '') -> _PersistentTask | WorkerSnapshot:
        with self.lock:
            record = self.tasks.get(spec.task_id)
            if record is None:
                record = _PersistentTask(spec=spec)
                self.tasks[spec.task_id] = record
            previous = record.operations.get(operation_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise OperationConflict('operation id was reused with different content')
                return previous[1]
            if previous is None and task_spec_to_dict(record.spec) != task_spec_to_dict(spec):
                raise StateTransitionError('task has already been started')
            return self._operation(record, operation_id, fingerprint, allowed={'starting'}, owner=owner, command='start')

    def reserve(
        self, task_id: str, operation_id: str, fingerprint: str, allowed: set[str], owner: str = '', command: str = ''
    ) -> _PersistentTask | WorkerSnapshot:
        with self.lock:
            record = self.tasks[task_id]
            return self._operation(record, operation_id, fingerprint, allowed=allowed, owner=owner, command=command)

    def reserve_progress(self, task_id: str, percent: int, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot:
        with self.lock:
            record = self.tasks[task_id]
            previous = record.operations.get(operation_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise OperationConflict('operation id was reused with different content')
                return previous[1]
            if percent < record.progress:
                raise StateTransitionError('progress cannot decrease')
            return self._operation(record, operation_id, fingerprint, allowed={'running'}, owner=owner, command='progress')

    def reserve_revise(self, spec: TaskSpec, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot:
        with self.lock:
            record = self.tasks[spec.task_id]
            previous = record.operations.get(operation_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise OperationConflict('operation id was reused with different content')
                return previous[1]
            if spec.revision <= record.spec.revision:
                raise StateTransitionError('revision must increase')
            return self._operation(record, operation_id, fingerprint, allowed={'running', 'paused'}, owner=owner, command='revise')

    def reserve_result(self, task_id: str, revision: int, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot:
        with self.lock:
            record = self.tasks[task_id]
            previous = record.operations.get(operation_id)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise OperationConflict('operation id was reused with different content')
                return previous[1]
            if revision != record.spec.revision:
                raise StateTransitionError('stale result revision')
            return self._operation(record, operation_id, fingerprint, allowed={'running'}, owner=owner, command='result')

    def _operation(
        self, record: _PersistentTask, operation_id: str, fingerprint: str, allowed: set[str] | None = None,
        allow_pending: bool = True, owner: str = '', command: str = '',
    ) -> _PersistentTask | WorkerSnapshot:
        previous = record.operations.get(operation_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise OperationConflict('operation id was reused with different content')
            return previous[1]
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError('operation_id must be a non-empty string')
        now = self._now()
        if record.pending is not None and record.pending != operation_id and record.claim_until is not None and now < record.claim_until:
            raise WorkerProtocolError('another operation is in progress')
        if record.pending == operation_id:
            if record.pending_fingerprint != fingerprint:
                raise OperationConflict('operation id was reused with different content')
            if record.claim_until is None or now >= record.claim_until:
                record.claim_owner, record.claim_until = owner, now + timedelta(seconds=self._lease_seconds)
                if record.pending_phase != 'external_succeeded':
                    record.pending_phase, record.pending_response = 'reserved', None
                return record
            if record.claim_owner != owner:
                raise WorkerProtocolError('operation is currently claimed')
            return record
        if allowed is not None and record.status not in allowed:
            raise StateTransitionError(f'{record.status} cannot accept this action')
        record.pending = operation_id
        record.pending_fingerprint = fingerprint
        record.pending_phase = 'reserved'
        record.previous_status = record.status
        record.claim_owner, record.claim_until = owner, now + timedelta(seconds=self._lease_seconds)
        if command == 'cancel':
            record.status = 'cancel_requested'
        return record

    def _require_current_claim(self, record: _PersistentTask, operation_id: str, fingerprint: str, owner: str) -> None:
        if (
            record.pending != operation_id
            or record.pending_fingerprint != fingerprint
            or record.claim_owner != owner
            or record.claim_until is None
            or self._now() >= record.claim_until
        ):
            raise WorkerProtocolError('lost worker operation reservation')

    def external_succeeded(self, task_id: str, operation_id: str, fingerprint: str, owner: str, response: Mapping[str, object]) -> None:
        with self.lock:
            record = self.tasks[task_id]
            self._require_current_claim(record, operation_id, fingerprint, owner)
            record.pending_phase, record.pending_response = 'external_succeeded', dict(response)

    def commit(self, task_id: str, operation_id: str, fingerprint: str, owner: str, status: str, *, session: str | None = None, progress: int | None = None, spec: TaskSpec | None = None) -> WorkerSnapshot:
        with self.lock:
            record = self.tasks[task_id]
            self._require_current_claim(record, operation_id, fingerprint, owner)
            if session is not None:
                record.session = session
            if progress is not None:
                if progress < record.progress:
                    raise StateTransitionError('progress cannot decrease')
                record.progress = progress
            if spec is not None:
                record.spec = spec
            record.status = status
            snapshot = record.snapshot()
            record.operations[operation_id] = (fingerprint, snapshot)
            record.pending = record.pending_fingerprint = record.pending_phase = None
            record.pending_response = None
            record.previous_status = record.claim_owner = None
            record.claim_until = None
            return snapshot

    def abort_start(self, task_id: str, operation_id: str, owner: str) -> None:
        with self.lock:
            record = self.tasks[task_id]
            if record.pending == operation_id and record.claim_owner == owner and record.status == 'starting' and record.pending_phase != 'external_succeeded':
                self.tasks.pop(task_id, None)

    def abort(self, task_id: str, operation_id: str, owner: str) -> None:
        with self.lock:
            record = self.tasks[task_id]
            if record.pending == operation_id and record.claim_owner == owner:
                record.status = record.previous_status or record.status
                record.pending = record.pending_fingerprint = record.pending_phase = None
                record.pending_response = None
                record.previous_status = record.claim_owner = None
                record.claim_until = None

    def snapshot(self, task_id: str) -> WorkerSnapshot:
        with self.lock:
            return self.tasks[task_id].snapshot()


class WorkerStore(Protocol):
    """Durable operation-store boundary used by WorkerRuntime."""

    def reserve_start(self, spec: TaskSpec, operation_id: str, fingerprint: str, owner: str = '') -> _PersistentTask | WorkerSnapshot: ...
    def reserve(self, task_id: str, operation_id: str, fingerprint: str, allowed: set[str], owner: str = '', command: str = '') -> _PersistentTask | WorkerSnapshot: ...
    def reserve_progress(self, task_id: str, percent: int, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot: ...
    def reserve_revise(self, spec: TaskSpec, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot: ...
    def reserve_result(self, task_id: str, revision: int, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot: ...
    def external_succeeded(self, task_id: str, operation_id: str, fingerprint: str, owner: str, response: Mapping[str, object]) -> None: ...
    def commit(self, task_id: str, operation_id: str, fingerprint: str, owner: str, status: str, **kwargs: object) -> WorkerSnapshot: ...
    def abort_start(self, task_id: str, operation_id: str, owner: str) -> None: ...
    def abort(self, task_id: str, operation_id: str, owner: str) -> None: ...
    def snapshot(self, task_id: str) -> WorkerSnapshot: ...


class SQLiteWorkerStore(InMemoryWorkerStore):
    """SQLite-backed store; task state and operation phases survive restart."""

    def __init__(self, path: str, *, lease_seconds: float = 30.0, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(lease_seconds=lease_seconds, clock=clock)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._closed = False
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA busy_timeout=5000')
        self._db.executescript(
            'CREATE TABLE IF NOT EXISTS worker_tasks (task_id TEXT PRIMARY KEY, payload TEXT NOT NULL);'
        )
        self._restore()

    def close(self) -> None:
        with self.lock:
            if not self._closed:
                self._db.close()
                self._closed = True
                del self._db

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError('worker store is closed')

    def _restore(self) -> None:
        self.tasks.clear()
        for task_id, payload in self._db.execute('SELECT task_id, payload FROM worker_tasks'):
            data = json.loads(payload)
            claim_until = data['claim_until']
            record = _PersistentTask(spec=task_spec_from_dict(data['spec']), session=data['session'], status=data['status'], progress=data['progress'], pending=data['pending'], pending_fingerprint=data['pending_fingerprint'], pending_phase=data['pending_phase'], pending_response=data['pending_response'], previous_status=data['previous_status'], claim_owner=data['claim_owner'], claim_until=None if claim_until is None else datetime.fromisoformat(claim_until))
            record.operations = {
                key: (value['fingerprint'], WorkerSnapshot(**value['snapshot']))
                for key, value in data['operations'].items()
            }
            self.tasks[task_id] = record

    def _save(self, record: _PersistentTask) -> None:
        payload = {
            'spec': task_spec_to_dict(record.spec), 'session': record.session, 'status': record.status,
            'progress': record.progress, 'pending': record.pending, 'pending_fingerprint': record.pending_fingerprint,
            'pending_phase': record.pending_phase, 'pending_response': record.pending_response,
            'previous_status': record.previous_status, 'claim_owner': record.claim_owner,
            'claim_until': None if record.claim_until is None else record.claim_until.isoformat(),
            'operations': {key: {'fingerprint': value[0], 'snapshot': value[1].__dict__} for key, value in record.operations.items()},
        }
        self._db.execute('INSERT OR REPLACE INTO worker_tasks VALUES (?, ?)', (record.spec.task_id, json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)))

    def reserve_start(self, spec: TaskSpec, operation_id: str, fingerprint: str, owner: str = '') -> _PersistentTask | WorkerSnapshot:
        return self._mutate(spec.task_id, lambda: super(SQLiteWorkerStore, self).reserve_start(spec, operation_id, fingerprint, owner))  # type: ignore[return-value]

    def reserve(self, task_id: str, operation_id: str, fingerprint: str, allowed: set[str], owner: str = '', command: str = '') -> _PersistentTask | WorkerSnapshot:
        return self._mutate(task_id, lambda: super(SQLiteWorkerStore, self).reserve(task_id, operation_id, fingerprint, allowed, owner, command))

    def reserve_progress(self, task_id: str, percent: int, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot:
        return self._mutate(task_id, lambda: super(SQLiteWorkerStore, self).reserve_progress(task_id, percent, operation_id, fingerprint, owner))  # type: ignore[return-value]

    def reserve_revise(self, spec: TaskSpec, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot:
        return self._mutate(spec.task_id, lambda: super(SQLiteWorkerStore, self).reserve_revise(spec, operation_id, fingerprint, owner))  # type: ignore[return-value]

    def reserve_result(self, task_id: str, revision: int, operation_id: str, fingerprint: str, owner: str) -> _PersistentTask | WorkerSnapshot:
        return self._mutate(task_id, lambda: super(SQLiteWorkerStore, self).reserve_result(task_id, revision, operation_id, fingerprint, owner))  # type: ignore[return-value]

    def external_succeeded(self, task_id: str, operation_id: str, fingerprint: str, owner: str, response: Mapping[str, object]) -> None:
        self._mutate(task_id, lambda: super(SQLiteWorkerStore, self).external_succeeded(task_id, operation_id, fingerprint, owner, response))

    def commit(self, task_id: str, operation_id: str, fingerprint: str, owner: str, status: str, **kwargs: object) -> WorkerSnapshot:
        return self._mutate(task_id, lambda: super(SQLiteWorkerStore, self).commit(task_id, operation_id, fingerprint, owner, status, **kwargs))  # type: ignore[return-value]

    def abort(self, task_id: str, operation_id: str, owner: str) -> None:
        self._mutate(task_id, lambda: super(SQLiteWorkerStore, self).abort(task_id, operation_id, owner))

    def abort_start(self, task_id: str, operation_id: str, owner: str) -> None:
        def abort() -> None:
            durable = self.tasks[task_id]
            if durable.pending == operation_id and durable.claim_owner == owner and durable.status == 'starting' and durable.pending_phase != 'external_succeeded':
                self.tasks.pop(task_id, None)
                self._db.execute('DELETE FROM worker_tasks WHERE task_id=?', (task_id,))
        self._mutate(task_id, abort)

    def snapshot(self, task_id: str) -> WorkerSnapshot:
        with self.lock:
            self._ensure_open()
            self._db.execute('BEGIN')
            try:
                self._restore()
                result = super().snapshot(task_id)
                self._db.execute('COMMIT')
                return result
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def _mutate(self, task_id: str, action: Callable[[], object]) -> object:
        with self.lock:
            self._ensure_open()
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._restore()
                result = action()
                record = self.tasks.get(task_id)
                if record is not None:
                    self._save(record)
                self._db.execute('COMMIT')
                return result
            except BaseException:
                self._db.execute('ROLLBACK')
                raise


class WorkerRuntime:
    """Two-phase worker operations: reserve, call adapter unlocked, then persist."""

    def __init__(self, adapter: WorkerAdapter, *, model: str, store: WorkerStore | None = None, shadow: bool = False) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError('worker model must be supplied by model policy')
        self._adapter, self._model, self._store, self._shadow = adapter, model, store or InMemoryWorkerStore(), shadow
        self._owner = uuid.uuid4().hex

    def start(self, spec: TaskSpec, *, materials: Materials = Materials(), memory_packet: MemoryPacket = MemoryPacket(), operation_id: str) -> WorkerSnapshot:
        if not isinstance(materials, Materials) or not isinstance(memory_packet, MemoryPacket):
            raise ValueError('worker context must use Materials and MemoryPacket DTOs')
        if not set(materials.references).issubset(set(spec.references)):
            raise ValueError('materials must be explicit TaskSpec references')
        fingerprint = self._fingerprint('start', {'spec': task_spec_to_dict(spec), 'materials': [item.__dict__ for item in materials.references], 'memory': memory_packet.summaries})
        reserved = self._store.reserve_start(spec, operation_id, fingerprint, self._owner)
        if isinstance(reserved, WorkerSnapshot):
            return reserved
        logical = f'task/{spec.task_id}/r{spec.revision}'
        try:
            session = f'shadow:{logical}' if self._shadow else self._adapter.session_for(logical)
            if not self._shadow:
                if reserved.pending_phase != 'external_succeeded':
                    response = self._adapter.call(logical_session=logical, model=self._model, command='start', operation_id=operation_id, messages=({'role': 'user', 'content': {'task': task_spec_to_dict(spec), 'materials': [item.__dict__ for item in materials.references], 'memory_packet': list(memory_packet.summaries)}},))
                    self._require_success(response)
                    self._store.external_succeeded(spec.task_id, operation_id, fingerprint, self._owner, self._strict_response(response))
        except BaseException:
            self._store.abort_start(spec.task_id, operation_id, self._owner)
            raise
        return self._store.commit(spec.task_id, operation_id, fingerprint, self._owner, 'running', session=session)

    def progress(self, task_id: str, percent: int, *, operation_id: str) -> WorkerSnapshot:
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError('progress must be 0..100')
        payload = {'percent': percent}
        fingerprint = self._fingerprint('progress', payload)
        reserved = self._store.reserve_progress(task_id, percent, operation_id, fingerprint, self._owner)
        return self._command(task_id, 'progress', payload, operation_id, {'running'}, progress=percent, reserved=reserved)

    def subtask(self, task_id: str, name: str, *, operation_id: str) -> WorkerSnapshot:
        self._require_text(name, 'subtask name')
        return self._command(task_id, 'subtask', {'name': name}, operation_id, {'running'})

    def pause(self, task_id: str, *, operation_id: str) -> WorkerSnapshot:
        return self._command(task_id, 'pause', {}, operation_id, {'running'}, status='paused')

    def resume(self, task_id: str, *, operation_id: str) -> WorkerSnapshot:
        return self._command(task_id, 'resume', {}, operation_id, {'paused'}, status='running')

    def cancel(self, task_id: str, *, operation_id: str) -> WorkerSnapshot:
        return self._command(task_id, 'cancel', {}, operation_id, {'running', 'paused'}, status='cancelled')

    def revise(self, spec: TaskSpec, *, operation_id: str) -> WorkerSnapshot:
        payload = {'spec': task_spec_to_dict(spec)}
        fingerprint = self._fingerprint('revise', payload)
        reserved = self._store.reserve_revise(spec, operation_id, fingerprint, self._owner)
        return self._command(spec.task_id, 'revise', payload, operation_id, {'running', 'paused'}, status='running', spec=spec, reserved=reserved, logical_revision=spec.revision)

    def result(self, task_id: str, revision: int, summary: str, *, operation_id: str) -> WorkerSnapshot:
        self._require_text(summary, 'result summary')
        payload = {'revision': revision, 'summary': summary}
        fingerprint = self._fingerprint('result', payload)
        reserved = self._store.reserve_result(task_id, revision, operation_id, fingerprint, self._owner)
        return self._command(task_id, 'result', payload, operation_id, {'running'}, status='completed', progress=100, reserved=reserved)

    def status(self, task_id: str) -> WorkerSnapshot:
        return self._store.snapshot(task_id)

    def _command(self, task_id: str, command: str, payload: Mapping[str, object], operation_id: str, allowed: set[str], *, status: str | None = None, progress: int | None = None, spec: TaskSpec | None = None, reserved: _PersistentTask | WorkerSnapshot | None = None, logical_revision: int | None = None) -> WorkerSnapshot:
        fingerprint = self._fingerprint(command, payload)
        if reserved is None:
            reserved = self._store.reserve(task_id, operation_id, fingerprint, allowed, self._owner, command)
        if isinstance(reserved, WorkerSnapshot):
            return reserved
        try:
            if not self._shadow:
                if reserved.pending_phase != 'external_succeeded':
                    response = self._adapter.call(logical_session=f'task/{task_id}/r{logical_revision if logical_revision is not None else reserved.spec.revision}', model=self._model, command=command, operation_id=operation_id, messages=({'role': 'user', 'content': dict(payload)},))
                    self._require_success(response)
                    self._store.external_succeeded(task_id, operation_id, fingerprint, self._owner, self._strict_response(response))
        except BaseException:
            self._store.abort(task_id, operation_id, self._owner)
            raise
        session = None
        if command == 'revise':
            logical = f'task/{task_id}/r{spec.revision}'
            session = f'shadow:{logical}' if self._shadow else self._adapter.session_for(logical)
        return self._store.commit(task_id, operation_id, fingerprint, self._owner, status or reserved.status, session=session, progress=progress if progress is not None else reserved.progress, spec=spec)

    @staticmethod
    def _fingerprint(action: str, payload: Mapping[str, object]) -> str:
        try:
            encoded = json.dumps({'action': action, 'payload': payload}, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError('worker payload must be strict JSON') from error
        return sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _require_success(response: object) -> None:
        if not isinstance(response, Mapping) or response.get('ok') is not True:
            raise WorkerProtocolError('worker adapter rejected command')

    @staticmethod
    def _strict_response(response: Mapping[str, object]) -> dict[str, object]:
        try:
            copied = json.loads(json.dumps(response, sort_keys=True, separators=(',', ':'), allow_nan=False))
        except (TypeError, ValueError) as error:
            raise WorkerProtocolError('worker adapter response must be strict JSON') from error
        if not isinstance(copied, dict):
            raise WorkerProtocolError('worker adapter response must be a JSON object')
        return copied

    @staticmethod
    def _require_text(value: object, field_name: str) -> None:
        if type(value) is not str or not value:
            raise ValueError(f'{field_name} must be a non-empty string')
