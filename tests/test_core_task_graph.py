"""Task graph, artifact, and restart-recovery contract tests."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from guanjia.core.contracts import ArtifactRef, TaskSpec
from guanjia.core.event_store import EventStore, IdempotencyConflict
from guanjia.core.task_repository import TaskRepository


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


with TemporaryDirectory() as directory:
    with EventStore(Path(directory) / 'graph.sqlite3') as store:
        repository = TaskRepository(store)
        repository.create(TaskSpec(task_id='parent', revision=1, goal='coordinate'), command_id='create-parent')
        try:
            repository.create(TaskSpec(task_id='bad\nid', revision=1, goal='invalid'), command_id='create-bad-id')
        except ValueError:
            pass
        else:
            raise AssertionError('task ids reject control characters')
        try:
            repository.create(
                TaskSpec(task_id='orphan', revision=1, goal='invalid'),
                parent_task_id='missing', command_id='create-orphan',
            )
        except KeyError:
            pass
        else:
            raise AssertionError('parent task must exist')
        try:
            repository.create(
                TaskSpec(task_id='self-parent', revision=1, goal='invalid'),
                parent_task_id='self-parent', command_id='create-self-parent',
            )
        except ValueError:
            pass
        else:
            raise AssertionError('task cannot be its own semantic parent')
        repository.create(TaskSpec(task_id='child', revision=1, goal='research'), command_id='create-child')
        try:
            repository.add_dependency('parent', 'missing')
        except KeyError:
            pass
        else:
            raise AssertionError('dependency endpoint must exist')
        repository.add_dependency('parent', 'child')
        check(repository.dependencies_of('parent') == ('child',), 'stores parent-child dependency')
        event_count = len(store.read(correlation_id='parent'))
        repository.add_dependency('parent', 'child')
        check(
            len(store.read(correlation_id='parent')) == event_count,
            'duplicate dependency does not emit another event',
        )
        try:
            repository.add_dependency('child', 'parent')
        except ValueError:
            pass
        else:
            raise AssertionError('dependency cycles rejected')
        artifact = ArtifactRef(uri='file:///report.md', kind='document')
        repository.add_artifact('child', artifact)
        repository.add_artifact('child', artifact)
        check(repository.artifacts_for('child') == (artifact,), 'stores outcome references')
        try:
            repository.add_artifact(
                'child', ArtifactRef(uri='file:///report.md', kind='different')
            )
        except IdempotencyConflict:
            pass
        else:
            raise AssertionError('artifact URI conflict rejected')
        repository.transition('child', 'scheduled', expected_revision=1, command_id='schedule-child')
        repository.transition('child', 'running', expected_revision=1, command_id='run-child')
        repository.transition('parent', 'scheduled', expected_revision=1, command_id='schedule-parent')
        repository.transition('parent', 'running', expected_revision=1, command_id='run-parent')
        repository.transition('parent', 'waiting_permission', expected_revision=1, command_id='wait-parent')
        for state in ('scheduled', 'running', 'pausing', 'waiting_dependency', 'verifying', 'cancel_requested'):
            task_id = f'recover-{state}'
            repository.create(TaskSpec(task_id=task_id, revision=1, goal='recover'), command_id=f'create-{task_id}')
            repository.transition(task_id, 'scheduled', expected_revision=1, command_id=f'schedule-{task_id}')
            if state not in {'scheduled'}:
                repository.transition(task_id, 'running', expected_revision=1, command_id=f'run-{task_id}')
            if state == 'pausing':
                repository.transition(task_id, 'pausing', expected_revision=1, command_id=f'pause-{task_id}')
            elif state == 'waiting_dependency':
                repository.transition(task_id, 'waiting_dependency', expected_revision=1, command_id=f'wait-dependency-{task_id}')
            elif state == 'verifying':
                repository.transition(task_id, 'verifying', expected_revision=1, command_id=f'verify-{task_id}')
            elif state == 'cancel_requested':
                repository.transition(task_id, 'cancel_requested', expected_revision=1, command_id=f'cancel-{task_id}')
        affected = repository.interrupt_incomplete(recovery_id='boot-1')
        check(
            {'child', 'parent'}.issubset(affected),
            'restart explicitly interrupts running and waiting',
        )
        check(
            repository.get('child').status == repository.get('parent').status == 'interrupted',
            'recovery state persisted',
        )
        check(
            all(repository.get(task_id).status == 'interrupted' for task_id in affected),
            'restart covers every non-final running state',
        )
        check(
            repository.interrupt_incomplete(recovery_id='boot-1') == (),
            'same recovery id is idempotent',
        )
        try:
            repository.interrupt_incomplete(recovery_id='bad\nrecovery')
        except ValueError:
            pass
        else:
            raise AssertionError('recovery identifiers reject control characters')
        repository.transition('child', 'scheduled', expected_revision=1, command_id='restore-child')
        check(
            repository.interrupt_incomplete(recovery_id='boot-2') == ('child',),
            'another recovery uses a distinct command and sequence',
        )

print('ALL PASS test_core_task_graph')
