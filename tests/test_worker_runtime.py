from __future__ import annotations

import sys
import tempfile
import gc
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.core.contracts import ArtifactRef, TaskSpec, task_spec_to_dict
from guanjia.core.worker_runtime import InMemoryWorkerStore, Materials, MemoryPacket, SQLiteWorkerStore, StateTransitionError, WorkerProtocolError, WorkerRuntime


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def session_for(self, logical_session: str) -> str:
        return f'oc:{logical_session}'

    def call(self, **request: object) -> dict[str, object]:
        self.calls.append(request)
        return {'ok': True, 'request_id': 'test'}


def spec(revision: int = 1, task_id: str = 'task-1') -> TaskSpec:
    return TaskSpec(task_id=task_id, revision=revision, goal='write a report', references=(ArtifactRef('file://brief', 'brief'),))


def test_worker_lifecycle_is_idempotent_and_context_is_minimal() -> None:
    adapter = RecordingAdapter()
    runtime = WorkerRuntime(adapter, model='policy-selected')
    start = runtime.start(spec(), materials=Materials((spec().references[0],)), memory_packet=MemoryPacket(('fact',)), operation_id='start-1')
    repeated = runtime.start(spec(), materials=Materials((spec().references[0],)), memory_packet=MemoryPacket(('fact',)), operation_id='start-1')
    assert start == repeated and start.session == 'oc:task/task-1/r1'
    assert adapter.calls[0]['messages'][0]['content']['task']['task_id'] == 'task-1'
    assert 'main_history' not in str(adapter.calls[0])

    assert runtime.progress('task-1', 20, operation_id='p1').status == 'running'
    assert runtime.subtask('task-1', 'outline', operation_id='s1').status == 'running'
    assert runtime.pause('task-1', operation_id='pause').status == 'paused'
    assert runtime.resume('task-1', operation_id='resume').status == 'running'
    assert runtime.result('task-1', 1, 'done', operation_id='result').status == 'completed'


def test_cancel_revise_race_rejects_old_results_and_terminal_controls_are_idempotent() -> None:
    runtime = WorkerRuntime(RecordingAdapter(), model='policy-selected')
    runtime.start(spec(), operation_id='start')
    revised = runtime.revise(spec(2), operation_id='revise')
    assert revised.revision == 2 and revised.status == 'running'
    try:
        runtime.result('task-1', 1, 'old', operation_id='old')
    except StateTransitionError:
        pass
    else:
        raise AssertionError('stale result was accepted')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(runtime.cancel, 'task-1', operation_id='cancel'),
            pool.submit(runtime.result, 'task-1', 2, 'new', operation_id='new'),
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except StateTransitionError:
                outcomes.append(None)
    assert runtime.status('task-1').status in {'cancelled', 'completed'}
    cancelled = outcomes[0]
    assert cancelled is not None and runtime.cancel('task-1', operation_id='cancel') == cancelled
    if runtime.status('task-1').status == 'cancelled':
        assert outcomes[1] is None


def test_shadow_worker_never_opens_session_or_calls_adapter() -> None:
    adapter = RecordingAdapter()
    runtime = WorkerRuntime(adapter, model='policy-selected', shadow=True)
    snapshot = runtime.start(spec(), operation_id='start')
    assert snapshot.status == 'running'
    assert not adapter.calls


def test_adapter_failure_is_retryable_operation_conflict_and_store_restores() -> None:
    class Flaky(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def call(self, **request: object) -> dict[str, object]:
            if self.fail:
                self.fail = False
                raise RuntimeError('down')
            return super().call(**request)

    adapter = Flaky()
    store = InMemoryWorkerStore()
    runtime = WorkerRuntime(adapter, model='policy-selected', store=store)
    try:
        runtime.start(spec(), operation_id='start')
    except RuntimeError:
        pass
    else:
        raise AssertionError('adapter failure was hidden')
    assert runtime.start(spec(), operation_id='start').status == 'running'
    restored = WorkerRuntime(adapter, model='policy-selected', store=store)
    assert restored.status('task-1').status == 'running'
    try:
        restored.progress('task-1', 1, operation_id='start')
    except Exception:
        pass
    else:
        raise AssertionError('operation id was reused for a different action')
    try:
        runtime.start(spec(), materials=Materials(), memory_packet=MemoryPacket(('nested main_history',)), operation_id='leak')
    except Exception:
        pass
    else:
        raise AssertionError('start id reuse with different semantics was accepted')


def test_sqlite_recovers_external_success_without_a_second_call() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        file = Path(directory) / 'worker.sqlite'
        adapter = RecordingAdapter()
        now = datetime(2026, 1, 1, tzinfo=UTC)

        def clock() -> datetime:
            return now

        store = SQLiteWorkerStore(str(file), lease_seconds=1, clock=clock)
        original = store.external_succeeded

        def crash_after_persist(*args: object) -> None:
            original(*args)  # type: ignore[arg-type]
            raise RuntimeError('simulated crash')

        store.external_succeeded = crash_after_persist  # type: ignore[method-assign]
        try:
            WorkerRuntime(adapter, model='policy-selected', store=store).start(spec(), operation_id='start')
        except RuntimeError:
            pass
        else:
            raise AssertionError('crash hook was not triggered')
        assert len(adapter.calls) == 1
        store.close()

        now += timedelta(seconds=1)
        restored_store = SQLiteWorkerStore(str(file), lease_seconds=1, clock=clock)
        restored = WorkerRuntime(adapter, model='policy-selected', store=restored_store)
        assert restored.start(spec(), operation_id='start').status == 'running'
        assert len(adapter.calls) == 1
        restored_store.close()


def test_sqlite_two_runtimes_only_one_claims_external_operation() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        file = Path(directory) / 'worker.sqlite'
        adapter = RecordingAdapter()
        first_store = SQLiteWorkerStore(str(file))
        second_store = SQLiteWorkerStore(str(file))
        first = WorkerRuntime(adapter, model='policy-selected', store=first_store)
        second = WorkerRuntime(adapter, model='policy-selected', store=second_store)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(runtime.start, spec(), operation_id='start') for runtime in (first, second)]
            outcomes = [future.exception() for future in futures]
        assert len(adapter.calls) == 1, adapter.calls
        assert all(outcome is None for outcome in outcomes), outcomes
        first_store.close()
        second_store.close()
        gc.collect()


def test_progress_regression_is_rejected_before_adapter_call() -> None:
    adapter = RecordingAdapter()
    runtime = WorkerRuntime(adapter, model='policy-selected')
    runtime.start(spec(), operation_id='start')
    runtime.progress('task-1', 50, operation_id='progress-50')
    calls_before = len(adapter.calls)
    try:
        runtime.progress('task-1', 49, operation_id='progress-49')
    except StateTransitionError:
        pass
    else:
        raise AssertionError('progress regression was accepted')
    assert len(adapter.calls) == calls_before


def test_sqlite_expired_lease_can_be_taken_over_and_close_is_explicit() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def clock() -> datetime:
        return now

    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / 'worker.sqlite')
        first_store = SQLiteWorkerStore(path, lease_seconds=5, clock=clock)
        second_store = SQLiteWorkerStore(path, lease_seconds=5, clock=clock)
        fingerprint = WorkerRuntime._fingerprint('start', {'spec': task_spec_to_dict(spec()), 'materials': [], 'memory': ()})
        first_store.reserve_start(spec(), 'start', fingerprint, 'first')
        now += timedelta(seconds=5)
        second = WorkerRuntime(RecordingAdapter(), model='policy-selected', store=second_store)
        assert second.start(spec(), operation_id='start').status == 'running'
        first_store.close()
        try:
            first_store.snapshot('task-1')
        except RuntimeError:
            pass
        else:
            raise AssertionError('closed worker store accepted a query')
        second_store.close()


def test_barrier_revise_rejects_old_result_and_uses_new_revision_session() -> None:
    class BlockingAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Barrier(2)
            self.release = Barrier(2)

        def call(self, **request: object) -> dict[str, object]:
            self.calls.append(request)
            if request['command'] == 'revise':
                self.entered.wait()
                self.release.wait()
            return {'ok': True, 'request_id': 'test'}

    adapter = BlockingAdapter()
    runtime = WorkerRuntime(adapter, model='policy-selected')
    runtime.start(spec(), operation_id='start')
    with ThreadPoolExecutor(max_workers=1) as pool:
        revise = pool.submit(runtime.revise, spec(2), operation_id='revise-2')
        adapter.entered.wait()
        try:
            runtime.result('task-1', 1, 'old result', operation_id='old-result')
        except WorkerProtocolError:
            pass
        else:
            raise AssertionError('old result was accepted while revise was reserved')
        adapter.release.wait()
        assert revise.result().revision == 2
    revise_call = next(call for call in adapter.calls if call['command'] == 'revise')
    assert revise_call['logical_session'] == 'task/task-1/r2'


def test_barrier_higher_revision_cannot_be_rolled_back_by_lower_revision() -> None:
    class BlockingAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Barrier(2)
            self.release = Barrier(2)

        def call(self, **request: object) -> dict[str, object]:
            self.calls.append(request)
            if request['command'] == 'revise':
                self.entered.wait()
                self.release.wait()
            return {'ok': True, 'request_id': 'test'}

    adapter = BlockingAdapter()
    runtime = WorkerRuntime(adapter, model='policy-selected')
    runtime.start(spec(), operation_id='start')
    with ThreadPoolExecutor(max_workers=1) as pool:
        revise_three = pool.submit(runtime.revise, spec(3), operation_id='revise-3')
        adapter.entered.wait()
        try:
            runtime.revise(spec(2), operation_id='revise-2')
        except WorkerProtocolError:
            pass
        else:
            raise AssertionError('lower revision displaced the reserved higher revision')
        adapter.release.wait()
        assert revise_three.result().revision == 3
    try:
        runtime.revise(spec(2), operation_id='revise-2')
    except StateTransitionError:
        pass
    else:
        raise AssertionError('lower revision rolled back a committed higher revision')


def test_expired_owner_cannot_persist_after_reclaim() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def clock() -> datetime:
        return now

    store = InMemoryWorkerStore(lease_seconds=1, clock=clock)
    fingerprint = WorkerRuntime._fingerprint('start', {'spec': task_spec_to_dict(spec()), 'materials': [], 'memory': ()})
    store.reserve_start(spec(), 'start', fingerprint, 'old-owner')
    now += timedelta(seconds=1)
    store.reserve_start(spec(), 'start', fingerprint, 'new-owner')
    try:
        store.external_succeeded('task-1', 'start', fingerprint, 'old-owner', {'ok': True})
    except WorkerProtocolError:
        pass
    else:
        raise AssertionError('expired owner persisted its response')


def test_barrier_progress_never_sends_a_lower_update() -> None:
    class BlockingAdapter(RecordingAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.entered = Barrier(2)
            self.release = Barrier(2)

        def call(self, **request: object) -> dict[str, object]:
            self.calls.append(request)
            if request['command'] == 'progress':
                self.entered.wait()
                self.release.wait()
            return {'ok': True, 'request_id': 'test'}

    adapter = BlockingAdapter()
    runtime = WorkerRuntime(adapter, model='policy-selected')
    runtime.start(spec(), operation_id='start')
    with ThreadPoolExecutor(max_workers=1) as pool:
        high = pool.submit(runtime.progress, 'task-1', 80, operation_id='high')
        adapter.entered.wait()
        calls_before = len(adapter.calls)
        try:
            runtime.progress('task-1', 20, operation_id='low')
        except WorkerProtocolError:
            pass
        else:
            raise AssertionError('lower progress displaced the reserved higher progress')
        assert len(adapter.calls) == calls_before
        adapter.release.wait()
        assert high.result().progress == 80
    try:
        runtime.progress('task-1', 20, operation_id='low')
    except StateTransitionError:
        pass
    else:
        raise AssertionError('lower progress was sent after higher progress committed')
