"""Task-revision and durable-projection contract tests."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from threading import Thread

from guanjia.core.contracts import (
    DeliveryRequirements, Evidence, PermissionScope, ResourceRequest, TaskResult, TaskSpec, WorkerEvent,
)
from guanjia.core.event_store import EventStore
from guanjia.core.task_repository import TaskRepository


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


with TemporaryDirectory() as directory:
    with EventStore(Path(directory) / 'tasks.sqlite3') as store:
        repository = TaskRepository(store)
        spec = TaskSpec(
            task_id='task-1', revision=1, goal='draft report', constraints=['local-only'],
            permission_scope=PermissionScope('write report', grants=['file.write']),
            priority=4, success_criteria=['has summary'],
            delivery_requirements=DeliveryRequirements(channels=['panel'], must_be_visible=True),
            resources=[ResourceRequest('desktop', units=1)],
        )
        repository.create(spec, command_id='create-task-1')
        for invalid_command in ('', 'bad\ncommand', 'x' * 201):
            try:
                repository.create(
                    TaskSpec(task_id=f'invalid-{len(invalid_command)}', revision=1, goal='invalid'),
                    command_id=invalid_command,
                )
            except ValueError:
                pass
            else:
                raise AssertionError('command identifiers reject controls and oversized input')
        check(
            repository.create(spec, command_id='create-task-1').revision == 1,
            'same create command is idempotent',
        )
        restored = repository.get('task-1').spec
        check(restored == spec, 'full TaskSpec persists and restores')
        try:
            Evidence('test', 'naive', observed_at=datetime(2026, 1, 1))
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError('naive timestamps are rejected')
        mutable_details = {'nested': {'value': 1}}
        frozen_evidence = Evidence('test', 'frozen', details=mutable_details)
        mutable_details['nested']['value'] = 2
        check(
            frozen_evidence.details['nested']['value'] == 1,
            'DTO mappings are deeply frozen and isolated',
        )
        try:
            WorkerEvent('task-1', 1, 'progress', 'x', emitted_at=datetime(2026, 1, 1))
        except ValueError:
            pass
        else:
            raise AssertionError('worker events require UTC timestamps')
        try:
            repository.transition('task-1', 'scheduled', expected_revision=1)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError('transitions require explicit command ids')
        repository.transition('task-1', 'scheduled', expected_revision=1, command_id='schedule-1')
        repository.transition('task-1', 'running', expected_revision=1, command_id='run-1')
        repository.transition('task-1', 'waiting_permission', expected_revision=1, command_id='wait-1')
        repository.transition('task-1', 'running', expected_revision=1, command_id='resume-1')
        check(
            repository.transition('task-1', 'running', expected_revision=1, command_id='resume-1').status == 'running',
            'the same transition command replays idempotently',
        )
        repository.revise('task-1', goal='draft corrected report', command_id='revise-2')
        check(
            repository.revise('task-1', goal='draft corrected report', command_id='revise-2').revision == 2,
            'same revise command cannot create an extra revision',
        )
        check(
            repository.spec_for('task-1', 1) == spec and repository.spec_for('task-1', 2).goal == 'draft corrected report',
            'every revision retains its complete TaskSpec',
        )
        try:
            repository.revise('task-1', goal='', command_id='empty-goal')
        except ValueError:
            pass
        else:
            raise AssertionError('an explicit empty revision goal is rejected')
        stale = TaskResult(
            result_id='result-stale',
            task_id='task-1',
            revision=1,
            summary='old report',
            evidence=(Evidence(kind='file', summary='old draft'),),
        )
        projection = repository.record_result(stale)
        check(
            projection.revision == 2 and projection.status == 'running',
            'stale revision result cannot update current state',
        )
        check(len(repository.evidence_for('task-1')) == 1, 'stale result remains evidence')
        current = TaskResult(
            result_id='result-current', task_id='task-1', revision=2, summary='correct report',
            evidence=(Evidence('observation', 'same content', evidence_id='observation-a'),),
        )
        check(
            repository.record_result(current).status == 'verifying',
            'current result moves task to verification',
        )
        result_events = store.read(correlation_id='task-1')
        result_index = next(
            index for index, event in enumerate(result_events)
            if event.envelope.event_type == 'task.result_received'
            and event.envelope.payload['result']['result_id'] == 'result-current'
        )
        state_event = result_events[result_index + 1].envelope
        result_payload = result_events[result_index].envelope.payload['result']
        check(
            state_event.event_type == 'task.state_changed'
            and state_event.payload['from'] == 'running'
            and state_event.payload['to'] == 'verifying',
            'accepted result is followed by an event-sourced state transition',
        )
        check(
            result_payload['evidence'][0]['evidence_id'] == 'observation-a'
            and result_payload['evidence'][0]['content_hash'] == current.evidence[0].content_hash,
            'result event preserves evidence identity for event-stream recovery',
        )
        try:
            repository.record_result(
                TaskResult(
                    result_id='result-current', task_id='task-1', revision=2, summary='correct report',
                    evidence=(Evidence('observation', 'same content', evidence_id='observation-other'),),
                )
            )
        except Exception:
            pass
        else:
            raise AssertionError('same result id with different evidence identity conflicts')
        repository.record_result(
            TaskResult(
                result_id='result-evidence-b', task_id='task-1', revision=2, summary='late',
                evidence=(Evidence('observation', 'same content', evidence_id='observation-b'),),
            )
        )
        check(
            len(repository.evidence_for('task-1')) == 3,
            'different evidence ids retain independent observations with equal content',
        )
        try:
            repository.record_result(
                TaskResult(
                    result_id='result-evidence-conflict', task_id='task-1', revision=2, summary='late',
                    evidence=(Evidence('observation', 'different', evidence_id='observation-b'),),
                )
            )
        except Exception:
            pass
        else:
            raise AssertionError('same evidence id with different content conflicts')
        repository.transition('task-1', 'succeeded', expected_revision=2, command_id='succeed-2')
        check(repository.get('task-1').status == 'succeeded', 'terminal state persisted')
        late = TaskResult(
            result_id='result-late', task_id='task-1', revision=2, summary='late result',
            evidence=(Evidence('late', 'kept as evidence', observed_at=datetime.now(timezone.utc)),),
        )
        check(
            repository.record_result(late).status == 'succeeded'
            and len(repository.evidence_for('task-1')) == 4,
            'late terminal result never changes final state but keeps evidence',
        )
        check(
            repository.record_result(late).status == 'succeeded'
            and len(repository.evidence_for('task-1')) == 4,
            'same stable result id writes result effects once',
        )
        try:
            repository.record_result(
                TaskResult(result_id='result-late', task_id='task-1', revision=2, summary='changed')
            )
        except Exception:
            pass
        else:
            raise AssertionError('conflicting result id is rejected')
        cancellable = TaskSpec(task_id='cancelled-result', revision=1, goal='must not revive')
        repository.create(cancellable, command_id='create-cancelled-result')
        repository.transition('cancelled-result', 'scheduled', expected_revision=1, command_id='schedule-cancelled-result')
        repository.transition('cancelled-result', 'running', expected_revision=1, command_id='run-cancelled-result')
        repository.transition('cancelled-result', 'cancel_requested', expected_revision=1, command_id='cancel-requested')
        late_cancelled = repository.record_result(
            TaskResult(
                result_id='late-cancelled-result', task_id='cancelled-result', revision=1,
                summary='must remain evidence', artifacts=(),
                evidence=(Evidence('late', 'cancel wins'),),
            )
        )
        check(
            late_cancelled.status == 'cancel_requested'
            and len(repository.evidence_for('cancelled-result')) == 1,
            'late result while cancelling stores only evidence and cannot advance state',
        )
        concurrent = TaskSpec(task_id='concurrent', revision=1, goal='serialize command')
        repository.create(concurrent, command_id='create-concurrent')
        outcomes: list[str] = []

        def schedule_once() -> None:
            outcomes.append(
                repository.transition(
                    'concurrent', 'scheduled', expected_revision=1, command_id='same-schedule',
                ).status
            )

        threads = [Thread(target=schedule_once), Thread(target=schedule_once)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        check(outcomes == ['scheduled', 'scheduled'], 'concurrent replay of one command is idempotent')
        try:
            repository.transition(
                'concurrent', 'running', expected_revision=1, command_id='create-concurrent',
            )
        except Exception:
            pass
        else:
            raise AssertionError('command ids are unique across all commands of one task')

print('ALL PASS test_core_task_revisions')
