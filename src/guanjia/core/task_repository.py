"""任务事件和读取投影的同事务仓库。"""

from __future__ import annotations

import json
import uuid
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Mapping

from guanjia.contracts.events import EventEnvelope

from .contracts import (
    ArtifactRef, Evidence, TaskResult, TaskSpec, task_spec_from_dict, task_spec_to_dict,
)
from .event_store import EventStore, IdempotencyConflict
from .task_projection import TaskProjection

_TRANSITIONS = {
    'accepted': {'scheduled', 'cancelled', 'interrupted'},
    'scheduled': {'running', 'cancelled', 'interrupted'},
    'running': {'pausing', 'waiting_permission', 'waiting_dependency', 'verifying', 'failed', 'cancel_requested', 'cancelled', 'interrupted'},
    'pausing': {'scheduled', 'cancelled', 'interrupted'},
    'waiting_permission': {'scheduled', 'running', 'cancelled', 'interrupted'},
    'waiting_dependency': {'scheduled', 'running', 'cancelled', 'interrupted'},
    'verifying': {'succeeded', 'failed', 'unverifiable', 'cancelled', 'interrupted'},
    'cancel_requested': {'cancelled', 'interrupted'},
    'succeeded': set(),
    'failed': set(),
    'cancelled': set(),
    'interrupted': {'scheduled', 'cancelled'},
    'unverifiable': {'scheduled', 'cancelled'},
}
_FINAL_STATUSES = {'succeeded', 'failed', 'cancelled', 'interrupted', 'unverifiable'}
_RECOVERY_STATUSES = {
    'scheduled', 'running', 'pausing', 'waiting_permission',
    'waiting_dependency', 'verifying', 'cancel_requested',
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class TaskRepository:
    """仅处理任务领域状态，不启动 worker 或执行权限判定。"""

    def __init__(self, event_store: EventStore) -> None:
        self.events = event_store
        with self.events.transaction() as connection:
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_projection (
                task_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL,
                parent_task_id TEXT,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_dependencies (
                task_id TEXT NOT NULL,
                depends_on_task_id TEXT NOT NULL,
                PRIMARY KEY(task_id, depends_on_task_id),
                FOREIGN KEY(task_id) REFERENCES task_projection(task_id),
                FOREIGN KEY(depends_on_task_id) REFERENCES task_projection(task_id)
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_artifacts (
                task_id TEXT NOT NULL,
                uri TEXT NOT NULL,
                artifact_json TEXT NOT NULL,
                PRIMARY KEY(task_id, uri),
                FOREIGN KEY(task_id) REFERENCES task_projection(task_id)
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                evidence_id TEXT,
                evidence_json TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES task_projection(task_id)
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_spec_versions (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                PRIMARY KEY(task_id, revision),
                FOREIGN KEY(task_id) REFERENCES task_projection(task_id)
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_transition_commands (
                task_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                command_id TEXT NOT NULL,
                transition_sequence INTEGER NOT NULL,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                PRIMARY KEY(task_id, revision, command_id)
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_revision_commands (
                task_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                request_json TEXT NOT NULL,
                PRIMARY KEY(task_id, command_id)
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_create_commands (
                task_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                parent_task_id TEXT,
                PRIMARY KEY(task_id, command_id)
            )
            '''
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_commands (
                task_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY(task_id, command_id)
            )
            '''
            )
            revision_columns = {
                row['name'] for row in connection.execute('PRAGMA table_info(task_revision_commands)')
            }
            if 'request_json' not in revision_columns:
                connection.execute('ALTER TABLE task_revision_commands ADD COLUMN request_json TEXT')
            evidence_columns = {
                row['name'] for row in connection.execute('PRAGMA table_info(task_evidence)')
            }
            if 'evidence_id' not in evidence_columns:
                connection.execute('ALTER TABLE task_evidence ADD COLUMN evidence_id TEXT')
            for evidence in connection.execute(
                'SELECT id, evidence_id, evidence_json FROM task_evidence WHERE evidence_id IS NULL'
            ):
                value = json.loads(evidence['evidence_json'])
                evidence_id = value.get('evidence_id') or f'legacy:{evidence["id"]}'
                connection.execute(
                    'UPDATE task_evidence SET evidence_id = ? WHERE id = ?',
                    (evidence_id, evidence['id']),
                )
            connection.execute('DROP INDEX IF EXISTS task_evidence_identity')
            connection.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS task_evidence_id '
                'ON task_evidence(task_id, revision, evidence_id)'
            )
            connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS task_results (
                result_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES task_projection(task_id)
            )
            '''
            )

    def _event(self, event_type: str, task_id: str, key: str, payload: dict[str, object]) -> None:
        canonical = f'{event_type}\x00{task_id}\x00{key}'.encode('utf-8')
        event_key = f'cmd:{hashlib.sha256(canonical).hexdigest()}'
        self.events.append(EventEnvelope.create(event_type, task_id, event_key, payload))

    @staticmethod
    def _command_id(command_id: str | None, field_name: str = 'command_id') -> str:
        if (
            not isinstance(command_id, str)
            or not 1 <= len(command_id) <= 200
            or re.search(r'[\x00-\x1f\x7f]', command_id) is not None
        ):
            raise ValueError(f'{field_name} must be 1..200 characters without control characters')
        return command_id

    @staticmethod
    def _task_id(task_id: str) -> str:
        return TaskRepository._command_id(task_id, 'task_id')

    @staticmethod
    def _request_hash(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def _claim_command(
        self, connection: object, task_id: str, command_id: str, kind: str,
        request: object, metadata: Mapping[str, object] | None = None,
    ) -> bool:
        """同一 task 的所有命令共用命名空间；返回是否首次执行。"""
        request_hash = self._request_hash(request)
        metadata_json = json.dumps(dict(metadata or {}), sort_keys=True)
        existing = connection.execute(
            'SELECT kind, request_hash FROM task_commands WHERE task_id = ? AND command_id = ?',
            (task_id, command_id),
        ).fetchone()
        if existing is not None:
            if existing['kind'] != kind or existing['request_hash'] != request_hash:
                raise IdempotencyConflict(f'command {command_id!r} is already used by another request')
            return False
        connection.execute(
            '''
            INSERT INTO task_commands(task_id, command_id, kind, request_hash, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (task_id, command_id, kind, request_hash, metadata_json),
        )
        return True

    def create(
        self, spec: TaskSpec, parent_task_id: str | None = None, *, command_id: str
    ) -> TaskProjection:
        command_id = self._command_id(command_id)
        self._task_id(spec.task_id)
        if parent_task_id is not None:
            self._task_id(parent_task_id)
            if parent_task_id == spec.task_id:
                raise ValueError('task cannot be its own parent')
        now = _now()
        encoded_spec = json.dumps(task_spec_to_dict(spec), sort_keys=True)
        with self.events.transaction() as connection:
            first_execution = self._claim_command(
                connection, spec.task_id, command_id, 'create',
                {'spec': task_spec_to_dict(spec), 'parent_task_id': parent_task_id},
            )
            if parent_task_id is not None:
                parent = connection.execute(
                    'SELECT parent_task_id FROM task_projection WHERE task_id = ?', (parent_task_id,)
                ).fetchone()
                if parent is None:
                    raise KeyError(parent_task_id)
                ancestor = parent_task_id
                seen: set[str] = set()
                while ancestor is not None:
                    if ancestor == spec.task_id or ancestor in seen:
                        raise ValueError('task parent chain contains a cycle')
                    seen.add(ancestor)
                    row = connection.execute(
                        'SELECT parent_task_id FROM task_projection WHERE task_id = ?', (ancestor,)
                    ).fetchone()
                    ancestor = None if row is None else row['parent_task_id']
            replay = connection.execute(
                'SELECT spec_json, parent_task_id FROM task_create_commands WHERE task_id = ? AND command_id = ?',
                (spec.task_id, command_id),
            ).fetchone()
            if replay is not None:
                if replay['spec_json'] != encoded_spec or replay['parent_task_id'] != parent_task_id:
                    raise IdempotencyConflict(f'create command {command_id!r} has different content')
                return self.get(spec.task_id)
            if not first_execution:
                raise IdempotencyConflict(f'create command {command_id!r} has no matching create record')
            existing = connection.execute(
                'SELECT spec_json, parent_task_id FROM task_projection WHERE task_id = ?', (spec.task_id,)
            ).fetchone()
            if existing is not None:
                raise IdempotencyConflict(f'task {spec.task_id!r} already exists under another create command')
            connection.execute(
                '''
                INSERT INTO task_projection(
                    task_id, revision, goal, status, priority, parent_task_id, spec_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?, ?)
                ''',
                (
                    spec.task_id, spec.revision, spec.goal, spec.priority, parent_task_id,
                    encoded_spec,
                    _timestamp(now), _timestamp(now),
                ),
            )
            connection.execute(
                'INSERT INTO task_spec_versions(task_id, revision, spec_json) VALUES (?, ?, ?)',
                (spec.task_id, spec.revision, encoded_spec),
            )
            connection.execute(
                'INSERT INTO task_create_commands(task_id, command_id, spec_json, parent_task_id) VALUES (?, ?, ?, ?)',
                (spec.task_id, command_id, encoded_spec, parent_task_id),
            )
            self._event(
                'task.created', spec.task_id, f'task-created:{spec.task_id}:{command_id}',
                {'command_id': command_id, 'parent_task_id': parent_task_id, 'spec': task_spec_to_dict(spec)},
            )
        return self.get(spec.task_id)

    def get(self, task_id: str) -> TaskProjection:
        with self.events.transaction() as connection:
            row = connection.execute(
                'SELECT * FROM task_projection WHERE task_id = ?', (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskProjection(
            task_id=row['task_id'], revision=row['revision'], goal=row['goal'], status=row['status'],
            priority=row['priority'], parent_task_id=row['parent_task_id'],
            created_at=_parse_timestamp(row['created_at']), updated_at=_parse_timestamp(row['updated_at']),
            artifacts=self.artifacts_for(task_id), spec=task_spec_from_dict(json.loads(row['spec_json'])),
        )

    def transition(
        self, task_id: str, status: str, *, expected_revision: int, command_id: str
    ) -> TaskProjection:
        command_id = self._command_id(command_id)
        self._task_id(task_id)
        with self.events.transaction() as connection:
            self._transition_in_transaction(
                connection, task_id, status, expected_revision, command_id,
            )
        return self.get(task_id)

    def _transition_in_transaction(
        self, connection: object, task_id: str, status: str, expected_revision: int,
        command_id: str, *, event_type: str = 'task.state_changed', command_kind: str = 'transition',
    ) -> bool:
        """在同一 BEGIN IMMEDIATE 中完成命令查重、CAS、命令记录和事件。"""
        first_execution = self._claim_command(
            connection, task_id, command_id, command_kind,
            {'status': status, 'revision': expected_revision, 'event_type': event_type},
        )
        replay = connection.execute(
            '''
            SELECT to_status FROM task_transition_commands
            WHERE task_id = ? AND revision = ? AND command_id = ?
            ''',
            (task_id, expected_revision, command_id),
        ).fetchone()
        if replay is not None:
            if replay['to_status'] != status:
                raise IdempotencyConflict(f'command {command_id!r} has a different transition')
            return False
        if not first_execution:
            raise IdempotencyConflict(f'command {command_id!r} has no matching transition record')
        row = connection.execute(
            'SELECT revision, status FROM task_projection WHERE task_id = ?', (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row['revision'] != expected_revision:
            raise ValueError('task revision is stale')
        previous = row['status']
        if status not in _TRANSITIONS.get(previous, set()):
            raise ValueError(f'illegal task transition: {previous} -> {status}')
        cursor = connection.execute(
            '''
            UPDATE task_projection SET status = ?, updated_at = ?
            WHERE task_id = ? AND revision = ? AND status = ?
            ''',
            (status, _timestamp(_now()), task_id, expected_revision, previous),
        )
        if cursor.rowcount != 1:
            raise ValueError('task state changed concurrently')
        sequence = connection.execute(
            'SELECT COALESCE(MAX(transition_sequence), 0) + 1 AS value '
            'FROM task_transition_commands WHERE task_id = ? AND revision = ?',
            (task_id, expected_revision),
        ).fetchone()['value']
        connection.execute(
            '''
            INSERT INTO task_transition_commands(
                task_id, revision, command_id, transition_sequence, from_status, to_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (task_id, expected_revision, command_id, sequence, previous, status),
        )
        self._event(
            event_type, task_id, f'task-state:{task_id}:{expected_revision}:{command_id}',
            {
                'command_id': command_id, 'transition_sequence': sequence, 'revision': expected_revision,
                'from': previous, 'to': status,
            },
        )
        return True

    def revise(
        self, task_id: str, *, goal: str | None = None, spec: TaskSpec | None = None,
        command_id: str,
    ) -> TaskProjection:
        command_id = self._command_id(command_id)
        self._task_id(task_id)
        request_json = json.dumps(
            {
                'goal': goal,
                'spec': None if spec is None else task_spec_to_dict(spec),
            },
            sort_keys=True,
        )
        with self.events.transaction() as connection:
            first_execution = self._claim_command(
                connection, task_id, command_id, 'revise', json.loads(request_json),
            )
            replay = connection.execute(
                'SELECT request_json FROM task_revision_commands WHERE task_id = ? AND command_id = ?',
                (task_id, command_id),
            ).fetchone()
            if replay is not None:
                if replay['request_json'] != request_json:
                    raise IdempotencyConflict(f'revise command {command_id!r} has different content')
                return self.get(task_id)
            if not first_execution:
                raise IdempotencyConflict(f'revise command {command_id!r} has no matching revision record')
            row = connection.execute(
                'SELECT revision, status, spec_json FROM task_projection WHERE task_id = ?', (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row['status'] in _FINAL_STATUSES:
                raise ValueError('terminal task cannot be revised')
            previous_revision = row['revision']
            revision = previous_revision + 1
            current_spec = task_spec_from_dict(json.loads(row['spec_json']))
            replacement = spec if spec is not None else replace(
                current_spec, goal=current_spec.goal if goal is None else goal,
            )
            if replacement.task_id != task_id:
                raise ValueError('revised spec must keep the task id')
            replacement = replace(replacement, revision=revision)
            encoded_replacement = json.dumps(task_spec_to_dict(replacement), sort_keys=True)
            cursor = connection.execute(
                '''
                UPDATE task_projection SET revision = ?, goal = ?, priority = ?, spec_json = ?, updated_at = ?
                WHERE task_id = ? AND revision = ? AND status = ?
                ''',
                (
                    revision, replacement.goal, replacement.priority,
                    encoded_replacement,
                    _timestamp(_now()), task_id, previous_revision, row['status'],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError('task changed concurrently')
            connection.execute(
                'INSERT INTO task_spec_versions(task_id, revision, spec_json) VALUES (?, ?, ?)',
                (task_id, revision, encoded_replacement),
            )
            connection.execute(
                '''
                INSERT INTO task_revision_commands(task_id, command_id, revision, spec_json, request_json)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (task_id, command_id, revision, encoded_replacement, request_json),
            )
            self._event(
                'task.revised', task_id, f'task-revised:{task_id}:{command_id}',
                {'command_id': command_id, 'revision': revision, 'spec': task_spec_to_dict(replacement)},
            )
        return self.get(task_id)

    def spec_for(self, task_id: str, revision: int) -> TaskSpec:
        with self.events.transaction() as connection:
            row = connection.execute(
                'SELECT spec_json FROM task_spec_versions WHERE task_id = ? AND revision = ?',
                (task_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError((task_id, revision))
        return task_spec_from_dict(json.loads(row['spec_json']))

    def add_dependency(self, task_id: str, depends_on_task_id: str) -> None:
        with self.events.transaction() as connection:
            endpoints = connection.execute(
                'SELECT task_id FROM task_projection WHERE task_id IN (?, ?)',
                (task_id, depends_on_task_id),
            ).fetchall()
            found = {row['task_id'] for row in endpoints}
            if task_id not in found or depends_on_task_id not in found:
                raise KeyError(task_id if task_id not in found else depends_on_task_id)
            if task_id == depends_on_task_id or self._reachable(depends_on_task_id, task_id, connection):
                raise ValueError('task dependency cycle')
            cursor = connection.execute(
                'INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_task_id) VALUES (?, ?)',
                (task_id, depends_on_task_id),
            )
            if cursor.rowcount == 1:
                self._event(
                    'task.dependency_added', task_id, f'task-dependency:{task_id}:{depends_on_task_id}',
                    {'task_id': task_id, 'depends_on_task_id': depends_on_task_id},
                )

    def _reachable(self, start: str, target: str, connection: object | None = None) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                if connection is None:
                    pending.extend(self.dependencies_of(current))
                else:
                    rows = connection.execute(
                        'SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?',
                        (current,),
                    ).fetchall()
                    pending.extend(row['depends_on_task_id'] for row in rows)
        return False

    def dependencies_of(self, task_id: str) -> tuple[str, ...]:
        with self.events.transaction() as connection:
            rows = connection.execute(
                'SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY depends_on_task_id',
                (task_id,),
            ).fetchall()
        return tuple(row['depends_on_task_id'] for row in rows)

    def add_artifact(self, task_id: str, artifact: ArtifactRef) -> None:
        encoded = json.dumps(artifact.__dict__, sort_keys=True)
        with self.events.transaction() as connection:
            self._add_artifact(connection, task_id, artifact, encoded)

    def _add_artifact(
        self, connection: object, task_id: str, artifact: ArtifactRef, encoded: str | None = None
    ) -> bool:
        encoded = encoded or json.dumps(artifact.__dict__, sort_keys=True)
        row = connection.execute(
            'SELECT artifact_json FROM task_artifacts WHERE task_id = ? AND uri = ?',
            (task_id, artifact.uri),
        ).fetchone()
        if row is not None:
            if row['artifact_json'] != encoded:
                raise IdempotencyConflict(f'artifact URI {artifact.uri!r} has conflicting content')
            return False
        connection.execute(
            'INSERT INTO task_artifacts(task_id, uri, artifact_json) VALUES (?, ?, ?)',
            (task_id, artifact.uri, encoded),
        )
        self._event(
            'task.artifact_added', task_id, f'task-artifact:{task_id}:{artifact.uri}',
            {'artifact': artifact.__dict__.copy()},
        )
        return True

    def artifacts_for(self, task_id: str) -> tuple[ArtifactRef, ...]:
        with self.events.transaction() as connection:
            rows = connection.execute(
                'SELECT artifact_json FROM task_artifacts WHERE task_id = ? ORDER BY uri', (task_id,)
            ).fetchall()
        return tuple(ArtifactRef(**json.loads(row['artifact_json'])) for row in rows)

    def record_result(self, result: TaskResult) -> TaskProjection:
        with self.events.transaction() as connection:
            row = connection.execute(
                'SELECT * FROM task_projection WHERE task_id = ?', (result.task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(result.task_id)
            projection = self._projection_from_row(row)
            result_json = self._result_json(result)
            result_command = f'result:{hashlib.sha256(result.result_id.encode("utf-8")).hexdigest()}'
            first_execution = self._claim_command(
                connection, result.task_id, result_command, 'result',
                json.loads(result_json), {'result_id': result.result_id},
            )
            previous = connection.execute(
                'SELECT result_json FROM task_results WHERE result_id = ?', (result.result_id,)
            ).fetchone()
            if previous is not None:
                if previous['result_json'] != result_json:
                    raise IdempotencyConflict(f'result_id {result.result_id!r} has conflicting content')
                return projection
            if not first_execution:
                raise IdempotencyConflict(f'result command for {result.result_id!r} has no matching result')
            connection.execute(
                'INSERT INTO task_results(result_id, task_id, result_json) VALUES (?, ?, ?)',
                (result.result_id, result.task_id, result_json),
            )
            eligible = result.revision == projection.revision and projection.status == 'running'
            stale = not eligible
            self._event(
                'task.result_received', result.task_id, f'task-result:{result.result_id}',
                {
                    'revision': result.revision, 'stale': stale, 'summary': result.summary,
                    'result': json.loads(result_json),
                },
            )
            for evidence in result.evidence:
                self._record_evidence(connection, result.task_id, result.revision, evidence)
            if eligible:
                for artifact in result.artifacts:
                    self._add_artifact(connection, result.task_id, artifact)
                new_status = 'failed' if result.error is not None else 'verifying'
                state_command = (
                    f'result-state:{hashlib.sha256(result.result_id.encode("utf-8")).hexdigest()}'
                )
                self._transition_in_transaction(
                    connection, result.task_id, new_status, result.revision, state_command,
                    command_kind='result_state',
                )
        return self.get(result.task_id)

    def _projection_from_row(self, row: object) -> TaskProjection:
        task_id = row['task_id']
        return TaskProjection(
            task_id=task_id, revision=row['revision'], goal=row['goal'], status=row['status'],
            priority=row['priority'], parent_task_id=row['parent_task_id'],
            created_at=_parse_timestamp(row['created_at']), updated_at=_parse_timestamp(row['updated_at']),
            artifacts=self.artifacts_for(task_id), spec=task_spec_from_dict(json.loads(row['spec_json'])),
        )

    @staticmethod
    def _result_json(result: TaskResult) -> str:
        return json.dumps(
            {
                'result_id': result.result_id, 'task_id': result.task_id, 'revision': result.revision,
                'summary': result.summary, 'artifacts': [artifact.__dict__ for artifact in result.artifacts],
                'evidence': [
                    {
                        'evidence_id': item.evidence_id, 'content_hash': item.content_hash,
                        'kind': item.kind, 'summary': item.summary,
                        'observed_at': _timestamp(item.observed_at),
                        'artifacts': [artifact.__dict__ for artifact in item.artifacts],
                        'details': _json_value(item.details),
                    } for item in result.evidence
                ],
                'error': None if result.error is None else {
                    'code': result.error.code, 'message': result.error.message,
                    'retryable': result.error.retryable, 'details': _json_value(result.error.details),
                },
                'key_steps': list(result.key_steps), 'evidence_at': _timestamp(result.evidence_at),
            },
            sort_keys=True,
        )

    def _record_evidence(self, connection: object, task_id: str, revision: int, evidence: Evidence) -> None:
        encoded = json.dumps(
            {
                'evidence_id': evidence.evidence_id, 'content_hash': evidence.content_hash,
                'kind': evidence.kind, 'summary': evidence.summary,
                'observed_at': _timestamp(evidence.observed_at),
                'artifacts': [artifact.__dict__ for artifact in evidence.artifacts],
                'details': _json_value(evidence.details),
            },
            sort_keys=True,
        )
        previous = connection.execute(
            '''
            SELECT evidence_json FROM task_evidence
            WHERE task_id = ? AND revision = ? AND evidence_id = ?
            ''',
            (task_id, revision, evidence.evidence_id),
        ).fetchone()
        if previous is not None:
            if previous['evidence_json'] != encoded:
                raise IdempotencyConflict(f'evidence_id {evidence.evidence_id!r} has conflicting content')
            return
        connection.execute(
            'INSERT INTO task_evidence(task_id, revision, evidence_id, evidence_json) VALUES (?, ?, ?, ?)',
            (task_id, revision, evidence.evidence_id, encoded),
        )

    def evidence_for(self, task_id: str) -> tuple[Evidence, ...]:
        with self.events.transaction() as connection:
            rows = connection.execute(
                'SELECT evidence_json FROM task_evidence WHERE task_id = ? ORDER BY id', (task_id,)
            ).fetchall()
        return tuple(
            Evidence(
                kind=value['kind'], summary=value['summary'],
                observed_at=_parse_timestamp(value['observed_at']),
                artifacts=tuple(ArtifactRef(**artifact) for artifact in value['artifacts']),
                details=value['details'], evidence_id=value.get('evidence_id'),
            )
            for row in rows
            for value in (json.loads(row['evidence_json']),)
        )

    def interrupt_incomplete(self, *, recovery_id: str | None = None) -> tuple[str, ...]:
        """重启恢复的明确命令：不伪装恢复 running/waiting 工作。"""
        recovery_id = recovery_id or f'recovery:{uuid.uuid4()}'
        self._command_id(recovery_id, 'recovery_id')
        with self.events.transaction() as connection:
            rows = connection.execute(
            """
            SELECT task_id, revision, status FROM task_projection
            WHERE status IN ('scheduled', 'running', 'pausing', 'waiting_permission',
                'waiting_dependency', 'verifying', 'cancel_requested')
            ORDER BY task_id
            """
            ).fetchall()
            affected: list[str] = []
            for row in rows:
                command_id = f'{recovery_id}:{row["task_id"]}:{row["revision"]}'
                changed = self._transition_in_transaction(
                    connection, row['task_id'], 'interrupted', row['revision'], command_id,
                    event_type='task.interrupted_on_recovery', command_kind='recovery',
                )
                if changed:
                    affected.append(row['task_id'])
        return tuple(affected)
