"""Progress aggregation contract tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from guanjia.core.contracts import WorkerEvent
from guanjia.core.progress import ProgressAggregator, ProgressPolicy


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


simple = ProgressAggregator(ProgressPolicy.simple())
try:
    WorkerEvent.progress('task', 1, 'bad-percent', percent=True)
except ValueError:
    pass
else:
    raise AssertionError('worker event rejects boolean percent')
check(
    simple.ingest(WorkerEvent.progress('task', 1, 'started', worker_event_id='simple-start', sequence=1)) is None,
    'simple hides intermediate progress',
)
check(
    simple.ingest(WorkerEvent.completed('task', 1, 'done', worker_event_id='simple-done', sequence=2)).kind == 'completed',
    'simple emits final event',
)
try:
    simple.ingest(WorkerEvent.progress('task', 1, 'after-terminal', worker_event_id='simple-after', sequence=3))
except ValueError:
    pass
else:
    raise AssertionError('events after a terminal package are rejected')

normal = ProgressAggregator(ProgressPolicy.normal())
base = datetime(2026, 10, 16, tzinfo=timezone.utc)
check(
    normal.ingest(WorkerEvent('task', 1, 'progress', 'started', emitted_at=base, worker_event_id='normal-1', sequence=1)) is not None,
    'normal emits first stage',
)
check(
    normal.ingest(WorkerEvent('task', 1, 'progress', 'working', emitted_at=base + timedelta(seconds=1), worker_event_id='normal-2', sequence=2))
    is not None,
    'stage transitions are immediately visible',
)

risky = ProgressAggregator(ProgressPolicy.long_high_risk())
first = risky.ingest(WorkerEvent('task', 1, 'progress', 'download', emitted_at=base, percent=10, worker_event_id='risk-1', sequence=1))
quiet = risky.ingest(
    WorkerEvent('task', 1, 'progress', 'download', emitted_at=base + timedelta(seconds=1), percent=20, worker_event_id='risk-2', sequence=2)
)
second = risky.ingest(
    WorkerEvent('task', 1, 'progress', 'download', emitted_at=base + timedelta(seconds=31), percent=55, worker_event_id='risk-3', sequence=3)
)
check(
    first is not None and quiet is None and second is not None and second.percent == 55
    and second.source_events == 2,
    'risk policy throttles unchanged stages and retains cumulative progress',
)
try:
    risky.ingest(WorkerEvent.progress('other-task', 1, 'started'))
except ValueError:
    pass
else:
    raise AssertionError('aggregator rejects another task revision')
check(
    risky.ingest(WorkerEvent('task', 1, 'progress', 'download', emitted_at=base + timedelta(seconds=31), percent=55, worker_event_id='risk-3', sequence=3)) is None,
    'worker event ids are idempotently deduplicated',
)
try:
    risky.ingest(
        WorkerEvent(
            'task', 1, 'progress', 'different', emitted_at=base + timedelta(seconds=32),
            percent=55, worker_event_id='risk-3', sequence=3,
        )
    )
except ValueError:
    pass
else:
    raise AssertionError('worker event id reuse with different semantics conflicts')
try:
    risky.ingest(WorkerEvent('task', 1, 'progress', 'download', emitted_at=base + timedelta(seconds=33), percent=30, worker_event_id='risk-4', sequence=2))
except ValueError:
    pass
else:
    raise AssertionError('worker event sequence cannot move backwards')
try:
    risky.ingest(WorkerEvent('task', 1, 'progress', 'download', emitted_at=base + timedelta(seconds=34), percent=30, worker_event_id='risk-5', sequence=4))
except ValueError:
    pass
else:
    raise AssertionError('progress percent cannot move backwards')

terminal = ProgressAggregator(ProgressPolicy.simple())
done = WorkerEvent.completed('terminal', 1, 'done', worker_event_id='done-1', sequence=1)
check(terminal.ingest(done) is not None and terminal.ingest(done) is None, 'terminal replay is idempotent')
try:
    terminal.ingest(WorkerEvent.completed('terminal', 1, 'other', worker_event_id='done-2', sequence=2))
except ValueError:
    pass
else:
    raise AssertionError('different terminal event is rejected')

anonymous = ProgressAggregator(ProgressPolicy.normal())
check(
    anonymous.ingest(WorkerEvent('anonymous', 1, 'progress', 'first', emitted_at=base)) is not None,
    'events without ids or sequences are accepted in non-decreasing emitted-at order',
)
try:
    anonymous.ingest(WorkerEvent('anonymous', 1, 'progress', 'older', emitted_at=base - timedelta(seconds=1)))
except ValueError:
    pass
else:
    raise AssertionError('anonymous worker event timestamps cannot move backwards')

print('ALL PASS test_core_progress')
