from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from guanjia.output import DeliveryRequirement, Expression, InMemoryOutputStore, OutputAuthorizationContext, OutputBroker, OutputRequest as _OutputRequest, OutputStatus, SQLiteOutputStore


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)

def OutputRequest(*args, **kwargs):
    actor_id = kwargs.get('actor_id', args[2] if len(args) > 2 else None)
    return _OutputRequest(*args, authorization=OutputAuthorizationContext(actor_id, kwargs.get('task_id'), 'output', 'output.send', 'conversation', 'reversible', 'test'), **kwargs)


class FailingChannel:
    name = 'voice'

    def deliver(self, request, expression, operation_id):
        raise RuntimeError('speaker unavailable')


class FallbackChannel:
    name = 'panel'

    def deliver(self, request, expression, operation_id):
        return 'panel-42'


class AcklessChannel:
    name = 'voice'

    def deliver(self, request, expression, operation_id):
        return 'voice-1'

class AllowAuthorizer:
    def allows(self, request):
        return True

broker = OutputBroker(InMemoryOutputStore(), {'voice': FailingChannel(), 'panel': FallbackChannel()}, AllowAuthorizer())
request = OutputRequest(
    output_id='out-fallback',
    dedupe_key='task-2:complete',
    actor_id='owner',
    conversation_id='conversation-2',
    text='已完成',
    artifact_refs=('artifact:report',),
    preferred_channels=('voice',),
    fallback_channels=('panel', 'panel', 'voice'),
)
receipt = broker.submit(request, Expression())
check(receipt.status is OutputStatus.FALLBACK, 'failed preferred channel enters fallback state')
check(receipt.channel == 'panel' and receipt.delivery_id == 'panel-42', 'fallback channel receives delivery')
check(request.preferred_channels == ('voice',) and request.fallback_channels == ('panel',), 'channels deduplicate in stable order')
attempts = broker.attempts_for('out-fallback')
check(
    [attempt.status for attempt in attempts] == ['failed', 'sent'],
    'attempt history preserves failed preferred and successful fallback',
)

failed = OutputBroker(InMemoryOutputStore(), {'voice': FailingChannel()}, AllowAuthorizer()).submit(
    OutputRequest(
        output_id='out-failed',
        dedupe_key='task-3:complete',
        actor_id='owner',
        conversation_id='conversation-3',
        text='失败',
        artifact_refs=(),
        preferred_channels=('voice',),
    ),
    Expression(),
)
check(failed.status is OutputStatus.FAILED and failed.failure_reason == 'RuntimeError', 'all failed channels remain failed with sanitized reason')

class DenyAuthorizer:
    def allows(self, request):
        return False

denied_channel = FallbackChannel()
denied = OutputBroker(InMemoryOutputStore(), {'panel': denied_channel}, DenyAuthorizer()).submit(
    OutputRequest('denied', 'denied', 'owner', 'conv', '敏感外发', (), ('panel',)),
    Expression(),
)
check(denied.status is OutputStatus.FAILED and not denied_channel.__dict__.get('attempts'), 'unauthorized output does not deliver')

now = datetime(2026, 10, 16, tzinfo=timezone.utc)
timeout_broker = OutputBroker(InMemoryOutputStore(), {'voice': AcklessChannel(), 'panel': FallbackChannel()}, AllowAuthorizer(), clock=lambda: now)
must_see = OutputRequest(
    'must-see', 'must-see', 'owner', 'conv', '请确认', (), ('voice',), ('panel',),
    delivery_requirement=DeliveryRequirement.MUST_BE_SEEN, ack_timeout_seconds=5,
)
timeout_broker.submit(must_see, Expression())
timeout = timeout_broker.handle_timeouts(now + timedelta(seconds=6))
check(timeout[0].status is OutputStatus.FALLBACK and timeout[0].channel == 'panel', 'unacknowledged must-see output falls back')


class IdempotentChannel:
    name = 'panel'

    def __init__(self, crash_once=False):
        self.operations = set()
        self.real_calls = 0
        self.crash_once = crash_once

    def deliver(self, request, expression, operation_id):
        if operation_id not in self.operations:
            self.operations.add(operation_id)
            self.real_calls += 1
        if self.crash_once:
            self.crash_once = False
            raise KeyboardInterrupt('simulated process crash')
        return f'ack:{operation_id}'


with TemporaryDirectory() as directory:
    database = Path(directory) / 'nested' / 'outputs.sqlite'
    durable_channel = IdempotentChannel()
    durable_request = OutputRequest('sqlite-submit', 'sqlite-submit', 'owner', 'conv', 'durable', (), ('panel',))
    first_store, second_store = SQLiteOutputStore(database), SQLiteOutputStore(database)
    first_broker = OutputBroker(first_store, {'panel': durable_channel}, AllowAuthorizer(), owner='one')
    second_broker = OutputBroker(second_store, {'panel': durable_channel}, AllowAuthorizer(), owner='two')
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda broker: broker.submit(durable_request, Expression()), (first_broker, second_broker)))
    check(durable_channel.real_calls == 1, 'two SQLite instances atomically submit and send once')
    first_store.close()
    second_store.close()

with TemporaryDirectory() as directory:
    database = Path(directory) / 'timeout.sqlite'
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    fallback_channel = IdempotentChannel()
    must_see = OutputRequest(
        'sqlite-timeout', 'sqlite-timeout', 'owner', 'conv', 'must see', (), ('voice',), ('panel',),
        delivery_requirement=DeliveryRequirement.MUST_BE_SEEN, ack_timeout_seconds=1,
    )
    initial_store = SQLiteOutputStore(database)
    OutputBroker(initial_store, {'voice': AcklessChannel(), 'panel': fallback_channel}, AllowAuthorizer(), clock=lambda: now, owner='initial').submit(must_see, Expression())
    initial_store.close()
    first_store, second_store = SQLiteOutputStore(database), SQLiteOutputStore(database)
    first_broker = OutputBroker(first_store, {'voice': AcklessChannel(), 'panel': fallback_channel}, AllowAuthorizer(), owner='timeout-one')
    second_broker = OutputBroker(second_store, {'voice': AcklessChannel(), 'panel': fallback_channel}, AllowAuthorizer(), owner='timeout-two')
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda broker: broker.handle_timeouts(now + timedelta(seconds=2)), (first_broker, second_broker)))
    check(fallback_channel.real_calls == 1, 'two brokers claim a due timeout fallback only once')
    first_store.close()
    second_store.close()

with TemporaryDirectory() as directory:
    database = Path(directory) / 'recovery.sqlite'
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    crash_channel = IdempotentChannel(crash_once=True)
    request = OutputRequest('crash', 'crash', 'owner', 'conv', 'recover', (), ('panel',))
    crashed_store = SQLiteOutputStore(database)
    try:
        OutputBroker(crashed_store, {'panel': crash_channel}, AllowAuthorizer(), clock=lambda: now, owner='crashed', lease_seconds=1).submit(request, Expression())
    except KeyboardInterrupt:
        pass
    else:
        check(False, 'crash phase is propagated without committing receipt')
    crashed_store.close()
    recovered_store = SQLiteOutputStore(database)
    recovered = OutputBroker(recovered_store, {'panel': crash_channel}, AllowAuthorizer(), clock=lambda: now + timedelta(seconds=2), owner='recovered', lease_seconds=1).submit(request, Expression())
    check(recovered.status is OutputStatus.SENT and crash_channel.real_calls == 1, 'rebuild takes expired claim with the same idempotency operation')
    check(OutputBroker(recovered_store, {'panel': crash_channel}, AllowAuthorizer()).mark_delivered('crash', recovered.delivery_id).delivered_at is not None, 'delivery acknowledgement records first delivery time')
    delivered_at = recovered_store.receipt('crash').delivered_at
    check(OutputBroker(recovered_store, {'panel': crash_channel}, AllowAuthorizer()).mark_delivered('crash', recovered.delivery_id).delivered_at == delivered_at, 'delivery acknowledgement is idempotent')
    recovered_store.close()

terminal_store = InMemoryOutputStore()
terminal_broker = OutputBroker(terminal_store, {'voice': AcklessChannel()}, AllowAuthorizer(), clock=lambda: now)
terminal_request = OutputRequest(
    'timeout-terminal', 'timeout-terminal', 'owner', 'conv', 'must acknowledge', (), ('voice',),
    delivery_requirement=DeliveryRequirement.MUST_BE_SEEN, ack_timeout_seconds=1,
)
terminal_broker.submit(terminal_request, Expression())
terminal_timeout = terminal_broker.handle_timeouts(now + timedelta(seconds=2))
check(
    terminal_timeout[0].status is OutputStatus.FAILED
    and terminal_broker.attempts_for('timeout-terminal')[-1].error == 'AckTimeout',
    'exhausted acknowledgement timeout records a terminal failure once',
)
check(not terminal_broker.handle_timeouts(now + timedelta(seconds=3)), 'terminal timeout is not due again')

print('ALL PASS test_output_delivery')
