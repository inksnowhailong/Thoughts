from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from guanjia.contracts import EventId
from guanjia.input.contracts import InputMessage
from guanjia.input.delivery import DeliveryQueue
from guanjia.input.store import InputStore


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
with TemporaryDirectory() as temporary:
    database = f'{temporary}/input.sqlite3'
    store = InputStore(database)
    message = InputMessage(EventId('input-1'), 'message-1', 'text', now)
    check(store.save_and_enqueue(message, '{"text":"hi"}', now), 'input and outbox enqueue atomically')
    queue = DeliveryQueue(database)
    check(queue.lease_next(now=now) is None, 'initial delivery waits one second')
    item = queue.lease_next(now=now + timedelta(seconds=1))
    check(item is not None, 'due message leased')
    queue.fail(item, 'offline', now=now + timedelta(seconds=1))
    check(queue.lease_next(now=now + timedelta(seconds=5)) is None, 'first failure waits five seconds')
    for offset in (6, 31):
        item = queue.lease_next(now=now + timedelta(seconds=offset))
        check(item is not None, 'retry becomes leasable')
        queue.fail(item, 'offline', now=now + timedelta(seconds=offset))
    check(queue.failed('input-1')['attempts'] == 3, 'third delivery failure enters failed queue')
    check(queue.replay('input-1', now=now), 'failed input replayed')
    replayed = queue.lease_next(now=now)
    check(replayed and replayed.input_id == 'input-1', 'replay preserves input id')
    queue.close()
    queue = DeliveryQueue(database)
    first_lease = replayed
    second_lease = queue.lease_next(now=now + timedelta(seconds=31), lease_seconds=1)
    check(
        not queue.acknowledge(first_lease) and queue.acknowledge(second_lease),
        'lease token prevents stale lease ABA acknowledgement',
    )
    queue.close()
    store.close()
print('ALL PASS test_input_delivery')
