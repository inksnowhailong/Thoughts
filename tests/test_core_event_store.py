"""SQLite event-journal contract tests."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from guanjia.contracts.events import EventEnvelope
from guanjia.core.event_store import EventStore, IdempotencyConflict


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


with TemporaryDirectory() as directory:
    database = Path(directory) / 'journal.sqlite3'
    event = EventEnvelope.create('task.created', 'task-1', 'create-task-1', {'goal': 'ship'})
    with EventStore(database) as store:
        first = store.append(event)
        duplicate = store.append(event)
        check(
            first.offset == duplicate.offset == 1 and first.created and not duplicate.created,
            'idempotent append exposes whether event was created',
        )
        conflicting = EventEnvelope.create(
            'task.created', 'task-1', 'create-task-1', {'goal': 'different'}
        )
        try:
            store.append(conflicting)
        except IdempotencyConflict:
            pass
        else:
            raise AssertionError('different envelope with same idempotency key conflicts')
        try:
            store.append(
                EventEnvelope.create(
                    'task.other', 'task-1', 'different-idempotency-key', {},
                    event_id=event.event_id,
                )
            )
        except IdempotencyConflict:
            pass
        else:
            raise AssertionError('event id collision is exposed as an idempotency conflict')
        check(store.watermark() == 1, 'watermark tracks committed events')
        check(
            store.read(correlation_id='task-1')[0].envelope == event,
            'reads by correlation id',
        )
        check(store.read(event_type='task.created')[0].envelope == event, 'reads by type')
        with store.transaction():
            second = store.append(
                EventEnvelope.create('task.running', 'task-1', 'run-task-1', {'revision': 1})
            )
        check(
            store.read(after_watermark=first.offset)[0].envelope == second.envelope,
            'reads after watermark',
        )
        for invalid_limit in (None, True, 0, -1, 1001):
            try:
                store.read(limit=invalid_limit)
            except ValueError:
                pass
            else:
                raise AssertionError('invalid read limit rejected')
        for invalid_watermark in (True, -1):
            try:
                store.read(after_watermark=invalid_watermark)
            except ValueError:
                pass
            else:
                raise AssertionError('invalid watermark rejected')
        inserted: list[int] = []

        def append_from_thread() -> None:
            threaded = EventEnvelope.create('task.threaded', 'task-1', 'threaded-1', {})
            inserted.append(store.append(threaded).offset)

        thread = Thread(target=append_from_thread)
        thread.start()
        thread.join()
        check(inserted == [3], 'shared connection is serialized safely across threads')
        try:
            with store.transaction():
                raise KeyboardInterrupt()
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError('BaseException leaves the transaction scope')
        check(
            store.append(EventEnvelope.create('task.recovered', 'task-1', 'recovered-1', {})).offset == 4,
            'BaseException rolls back and resets the transaction owner',
        )

    with EventStore(database) as reopened:
        check(reopened.watermark() == 4, 'journal survives reopening')

    with EventStore(database) as first_store, EventStore(database) as second_store:
        shared = EventEnvelope.create('task.shared', 'task-2', 'shared-command', {'revision': 1})
        offsets: list[int] = []

        def append_shared(store: EventStore) -> None:
            offsets.append(store.append(shared).offset)

        threads = [Thread(target=append_shared, args=(first_store,)), Thread(target=append_shared, args=(second_store,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        check(offsets[0] == offsets[1], 'separate EventStore instances replay the same command idempotently')

print('ALL PASS test_core_event_store')
