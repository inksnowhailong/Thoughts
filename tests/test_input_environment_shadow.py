from datetime import datetime, timedelta, timezone
import threading

from guanjia.input.adapters.environment import (
    EnvironmentReaders,
    EnvironmentShadowAdapter,
    EnvironmentShadowAsyncAdapter,
)
from guanjia.input.projections.diagnostics import InMemoryDiagnosticsProjection
from guanjia.input.state_cache import StateCache


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
calls = []


def reader(name, value):
    def read():
        calls.append(name)
        return value
    return read


projection = InMemoryDiagnosticsProjection()
adapter = EnvironmentShadowAdapter(
    StateCache(),
    EnvironmentReaders(
        program_context=reader('program_context', {'app': 'editor'}),
        program_quiet=reader('program_quiet', {'active': False}),
        runtime_state=reader('runtime_state', 'active'),
        service_health=reader('service_health', {'gateway': 'healthy'}),
    ),
    projection,
    ttl=timedelta(seconds=30),
)
context = adapter.collect_in_background('input-env-1', 'corr-env-1', now=now)
check(calls == ['program_context', 'program_quiet', 'runtime_state', 'service_health'], 'only injected readers run')
check(context.values['program_context'].value['app'] == 'editor', 'context is stored in StateCache')
records = projection.records()
check(len(records) == 1, 'diagnostic projection receives one snapshot')
check(records[0].input_id == 'input-env-1' and records[0].correlation_id == 'corr-env-1', 'global IDs retained')
try:
    records[0].context.values['program_context'].value['app'] = 'other'
except TypeError:
    pass
else:
    raise AssertionError('diagnostic state is deeply immutable')


def failing_health():
    raise RuntimeError('offline token=super-secret')


failed = EnvironmentShadowAdapter(
    StateCache(),
    EnvironmentReaders(
        program_context=reader('context-2', {}),
        program_quiet=reader('quiet-2', {}),
        runtime_state=reader('runtime-2', {}),
        service_health=failing_health,
    ),
    InMemoryDiagnosticsProjection(),
)
failed_context = failed.collect_in_background('input-env-2', 'corr-env-2', now=now)
check(failed_context.values['service_health'].status == 'unknown', 'reader failures are diagnostic observations')
check(
    failed_context.values['service_health'].observations[0].error == 'RuntimeError: <redacted>',
    'reader errors do not retain sensitive exception text',
)

utc_calls = []
try:
    adapter.collect_in_background('input-env-naive', 'corr-env-naive', now=datetime(2026, 10, 16))
except ValueError:
    pass
else:
    raise AssertionError('environment observation requires UTC before readers run')
check(calls.count('program_context') == 1, 'invalid UTC never invokes a reader')

concurrent_calls = []
concurrent_projection = InMemoryDiagnosticsProjection()
concurrent = EnvironmentShadowAdapter(
    StateCache(),
    EnvironmentReaders(
        program_context=reader('concurrent-context', {}),
        program_quiet=reader('concurrent-quiet', {}),
        runtime_state=reader('concurrent-runtime', {}),
        service_health=reader('concurrent-health', {}),
    ),
    concurrent_projection,
)
barrier = threading.Barrier(8)
contexts = []


def observe_same_input():
    barrier.wait()
    contexts.append(concurrent.collect_in_background('input-env-concurrent', 'corr-env-concurrent', now=now))


threads = [threading.Thread(target=observe_same_input) for _ in range(8)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check(len(concurrent_projection.records()) == 1 and len(contexts) == 8, 'concurrent duplicate environment input is idempotent')


class FailingProjection:
    def __init__(self):
        self.calls = 0

    def project(self, _record):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError('projection unavailable')


retry_cache = StateCache()
retry = EnvironmentShadowAdapter(
    retry_cache,
    EnvironmentReaders(
        program_context=lambda: {'token': 'secret'},
        program_quiet=lambda: {},
        runtime_state=lambda: {},
        service_health=lambda: {},
    ),
    FailingProjection(),
)
try:
    retry.collect_in_background('input-env-retry', 'corr-env-retry', now=now)
except RuntimeError:
    pass
else:
    raise AssertionError('projection failure remains visible to the background caller')
check(not retry_cache.snapshot(now=now).values, 'projection failure leaves no cache mutation')
retry.collect_in_background('input-env-retry', 'corr-env-retry', now=now)
check('program_context' in retry_cache.snapshot(now=now).values, 'failed projection input may be retried')
async_retry = EnvironmentShadowAsyncAdapter(
    EnvironmentShadowAdapter(
        StateCache(),
        EnvironmentReaders(lambda: {}, lambda: {}, lambda: {}, lambda: {}),
        FailingProjection(),
    ),
    max_queue_size=1,
)
check(async_retry.observe('input-env-async-retry', 'corr-env-async-retry', now=now), 'async projection failure is accepted')
check(async_retry.drain(timeout=1), 'async failure completes its first attempt')
check(async_retry.metrics().sink_failures == 1, 'async failure is isolated')
check(async_retry.observe('input-env-async-retry', 'corr-env-async-retry', now=now), 'async failure releases input claim for retry')
async_retry.close()

reader_started = threading.Event()
reader_release = threading.Event()


def blocked_reader():
    reader_started.set()
    reader_release.wait()
    return {}


background = EnvironmentShadowAdapter(
    StateCache(),
    EnvironmentReaders(blocked_reader, lambda: {}, lambda: {}, lambda: {}),
    InMemoryDiagnosticsProjection(),
)
async_adapter = EnvironmentShadowAsyncAdapter(background, max_queue_size=1)
started_at = datetime.now().timestamp()
check(async_adapter.observe('input-env-async', 'corr-env-async', now=now), 'async environment request is queued')
check(datetime.now().timestamp() - started_at < 0.1, 'blocked reader does not block source adapter')
check(reader_started.wait(1), 'background reader starts asynchronously')
check(async_adapter.close(drain=False, timeout=0.2), 'non-draining close returns despite blocked reader')
reader_release.set()
print('ALL PASS test_input_environment_shadow')
