from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Lock

from guanjia.output import (
    DeliveryAttempt, Expression, IdempotencyConflict, InMemoryOutputStore, OutputAuthorizationContext,
    OutputBroker, OutputRequest as _OutputRequest, OutputStatus, Receipt,
)


def check(value, label):
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


def OutputRequest(*args, **kwargs):
    actor_id = kwargs.get('actor_id', args[2] if len(args) > 2 else None)
    task_id = kwargs.get('task_id')
    return _OutputRequest(*args, authorization=OutputAuthorizationContext(
        actor_id, task_id, 'output', 'output.send', 'conversation', 'reversible', 'test',
    ), **kwargs)


class RecordingChannel:
    name = 'panel'

    def __init__(self):
        self.attempts = []

    def deliver(self, request, expression, operation_id):
        self.attempts.append((request, expression, operation_id))
        return 'delivery-1'


class AllowAuthorizer:
    def allows(self, request):
        return True


store = InMemoryOutputStore()
channel = RecordingChannel()
broker = OutputBroker(store, {'panel': channel}, AllowAuthorizer())
request = OutputRequest('out-1', 'task-1:complete', 'owner', 'conversation-1', '任务完成', (), ('panel',))
first = broker.submit(request, Expression(tone='calm'))
check(first.status is OutputStatus.SENT and len(channel.attempts) == 1, 'successful channel delivers once')
check(broker.submit(request, Expression()) == first and len(channel.attempts) == 1, 'dedupe returns original receipt')
check(broker.mark_delivered('out-1', 'delivery-1').status is OutputStatus.DELIVERED, 'matching receipt marks delivery')

try:
    broker.submit(OutputRequest('conflict', request.dedupe_key, 'owner', 'conversation-1', '不同内容', (), ('panel',)), Expression())
except IdempotencyConflict:
    check(True, 'dedupe conflict is rejected')
else:
    check(False, 'dedupe conflict is rejected')

for invalid in (
    lambda: Expression(emotion_intensity=float('nan')),
    lambda: Expression(rate=float('inf')),
    lambda: OutputRequest('timeout', 'timeout', 'owner', 'conv', 'text', (), ('panel',), ack_timeout_seconds=True),
    lambda: Receipt('bad', 'bad', OutputStatus.SENT),
    lambda: DeliveryAttempt('bad', 'panel', OutputStatus.SENT, datetime.now()),
):
    try:
        invalid()
    except ValueError:
        check(True, 'strict contract rejects invalid values')
    else:
        check(False, 'strict contract rejects invalid values')


class BlockingChannel(RecordingChannel):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.lock = Lock()

    def deliver(self, request, expression, operation_id):
        with self.lock:
            self.attempts.append((request, expression, operation_id))
        self.started.set()
        self.release.wait(timeout=2)
        return 'delivery-1'


parallel_channel = BlockingChannel()
parallel_store = InMemoryOutputStore()
default_broker = OutputBroker(parallel_store, {'panel': parallel_channel}, AllowAuthorizer())
same_owner_broker = OutputBroker(parallel_store, {'panel': parallel_channel}, AllowAuthorizer(), owner='same-owner')
parallel_request = OutputRequest('out-parallel', 'parallel', 'owner', 'conv', '一次', (), ('panel',))
with ThreadPoolExecutor(max_workers=2) as workers:
    first = workers.submit(default_broker.submit, parallel_request, Expression())
    check(parallel_channel.started.wait(timeout=1), 'worker reaches blocking channel')
    second = workers.submit(same_owner_broker.submit, parallel_request, Expression())
    check(len(parallel_channel.attempts) == 1, 'active lease prevents same-owner and default broker duplicate calls')
    parallel_channel.release.set()
    first.result()
    second.result()

print('ALL PASS test_output_broker')
