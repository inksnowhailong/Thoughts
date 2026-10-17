import threading
import time
import copy
from dataclasses import replace
from datetime import datetime, timezone

from guanjia.input.contracts import MediaRef
from guanjia.input.adapters._async_shadow import ProcessIdempotencyStore
from guanjia.input.adapters.voice import AcceptedInputIssuer, AcceptedVoiceSegment, VoiceShadowAdapter


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
issuer = AcceptedInputIssuer()


def segment(*args, **kwargs):
    return issuer.issue(*args, **kwargs)


try:
    segment('voice-naive', 'corr-naive', '错误时间', datetime(2026, 10, 16))
except ValueError:
    pass
else:
    raise AssertionError('voice DTO requires strict UTC timestamps')
received = []
started = threading.Event()
release = threading.Event()


def blocking_sink(record):
    received.append(record)
    started.set()
    release.wait(1)


adapter = VoiceShadowAdapter(blocking_sink, issuer=issuer, max_queue_size=1)
first = segment('voice-1', 'corr-1', '第一句', now)
second = segment('voice-2', 'corr-2', '第二句', now)
third = segment('voice-3', 'corr-3', '第三句', now)
check(adapter.observe(first), 'accepted voice segment is queued')
check(started.wait(1), 'worker receives the first accepted segment')
check(adapter.observe(second), 'one queued shadow segment fits')
start = time.monotonic()
check(not adapter.observe(third), 'full queue drops the shadow only')
check(time.monotonic() - start < 0.1, 'queue-full path never blocks voice')
check(not adapter.observe(first), 'duplicate input is idempotently ignored')
release.set()
check(adapter.drain(timeout=1), 'drain completes submitted shadow work')
check([record.input_id for record in received] == ['voice-1', 'voice-2'], 'only accepted unique segments reach sink')
check(adapter.metrics().dropped_queue_full == 1, 'queue-full loss is counted')
check(adapter.metrics().delivered == 2 and adapter.metrics().accepted == 2, 'accepted and delivered are distinct counters')


def failing_sink(_record):
    raise RuntimeError('sink failed')


retry_store = ProcessIdempotencyStore(max_entries=1)
failing = VoiceShadowAdapter(failing_sink, issuer=issuer, max_queue_size=1, idempotency_store=retry_store)
failed_segment = segment('voice-4', 'corr-4', '失败', now)
check(failing.observe(failed_segment), 'sink failure does not reject source input')
check(failing.drain(timeout=1), 'failing sink work is completed and isolated')
check(failing.metrics().sink_failures == 1, 'sink exceptions are counted')
check(failing.observe(failed_segment), 'sink failure releases the input claim for retry')
failing.close()
adapter.close()
check(not adapter.observe(segment('voice-5', 'corr-5', '关闭后', now)), 'closed adapter rejects without blocking')

eventual_started = threading.Event()
eventual_release = threading.Event()


def eventual_success(_record):
    eventual_started.set()
    eventual_release.wait()


eventual = VoiceShadowAdapter(eventual_success, issuer=issuer, max_queue_size=1, sink_timeout=0.01)
eventual_segment = segment('voice-eventual', 'corr-eventual', '延迟成功', now)
check(eventual.observe(eventual_segment), 'eventual sink input is accepted')
check(eventual_started.wait(1), 'eventual sink started')
time.sleep(0.03)
check(eventual.metrics().timed_out == 1, 'timeout is counted before final sink outcome')
check(not eventual.observe(eventual_segment), 'timed-out live invocation retains its input claim')
eventual_release.set()
check(eventual.drain(timeout=1), 'eventual completion clears inflight state')
check(eventual.metrics().delivered == 1, 'eventual sink success is counted separately')
eventual.close()

failed_started = threading.Event()
failed_release = threading.Event()
retry_after_close_store = ProcessIdempotencyStore()


def eventual_failure(_record):
    failed_started.set()
    failed_release.wait()
    raise RuntimeError('eventual failure')


closing = VoiceShadowAdapter(
    eventual_failure,
    issuer=issuer,
    max_queue_size=1,
    sink_timeout=0.01,
    idempotency_store=retry_after_close_store,
)
closing_segment = segment('voice-close-live', 'corr-close-live', '关闭中', now)
check(closing.observe(closing_segment), 'close-race input is accepted')
check(failed_started.wait(1), 'close-race sink started')
check(closing.close(drain=False, timeout=0.2), 'non-draining close detaches live invocation')
replacement = VoiceShadowAdapter(lambda _record: None, issuer=issuer, idempotency_store=retry_after_close_store)
check(not replacement.observe(closing_segment), 'non-draining close keeps live input claimed')
failed_release.set()
for _ in range(100):
    if replacement.observe(closing_segment):
        break
    time.sleep(0.01)
else:
    raise AssertionError('eventual sink failure must release the input claim')
replacement.close()


race = VoiceShadowAdapter(lambda _record: None, issuer=issuer, max_queue_size=2)
thread = threading.Thread(target=race.close)
thread.start()
race.observe(segment('voice-race', 'corr-race', '竞态', now))
thread.join(1)
check(not thread.is_alive(), 'close does not leak worker threads under observe race')

blocked_sink_started = threading.Event()
permanent_release = threading.Event()


def permanent_sink(_record):
    blocked_sink_started.set()
    permanent_release.wait()


permanent = VoiceShadowAdapter(permanent_sink, issuer=issuer, max_queue_size=1, sink_timeout=10)
check(permanent.observe(segment('voice-permanent', 'corr-permanent', '永久阻塞', now)), 'permanent sink record is accepted')
check(blocked_sink_started.wait(1), 'permanent sink starts in isolated daemon call')
started_at = time.monotonic()
check(permanent.close(drain=False, timeout=0.2), 'non-draining close returns despite permanent sink')
check(time.monotonic() - started_at < 0.2, 'close false does not wait on permanent sink')
check(permanent.metrics().dropped_on_close == 0, 'non-draining close does not drop a live sink invocation')

store = ProcessIdempotencyStore(max_entries=1)
check(store.claim('shared-id'), 'first shared-store claim succeeds')
check(not store.claim('shared-id'), 'shared-store duplicate claim is rejected')
check(store.claim('other-pending'), 'pending IDs are never evicted by completed capacity')
check(not store.claim('shared-id'), 'in-flight pending ID remains claimed at max_entries one')
store.release('shared-id')
check(store.claim('shared-id'), 'released failed claim can be retried')
store.complete('shared-id')
check(store.claim('completed-replacement'), 'only completed IDs are capacity-evicted')
shared_store = ProcessIdempotencyStore()
shared_a = VoiceShadowAdapter(lambda _record: None, issuer=issuer, idempotency_store=shared_store)
shared_b = VoiceShadowAdapter(lambda _record: None, issuer=issuer, idempotency_store=shared_store)
shared_barrier = threading.Barrier(2)
shared_outcomes = []


def observe_shared(adapter):
    shared_barrier.wait()
    shared_outcomes.append(adapter.observe(segment('voice-shared', 'corr-shared', '共享', now)))


shared_threads = [
    threading.Thread(target=observe_shared, args=(shared_a,)),
    threading.Thread(target=observe_shared, args=(shared_b,)),
]
for thread in shared_threads:
    thread.start()
for thread in shared_threads:
    thread.join()
check(sum(shared_outcomes) == 1, 'shared idempotency store atomically rejects a second adapter claim')
shared_a.close()
shared_b.close()

bounded = VoiceShadowAdapter(
    permanent_sink,
    issuer=issuer,
    max_queue_size=100,
    sink_timeout=0.01,
    max_outstanding_sink_calls=3,
)
for index in range(100):
    check(bounded.observe(segment(f'voice-leak-{index}', f'corr-leak-{index}', '阻塞', now)), 'bounded test input queues')
check(not bounded.drain(timeout=0.1), 'live permanent sink calls remain inflight after worker timeout')
check(bounded.metrics().circuit_open >= 97, 'outstanding blocked sink calls open the circuit')
alive_sink_calls = [thread for thread in threading.enumerate() if thread.name.endswith('-sink-call')]
check(len(alive_sink_calls) <= 4, 'permanent sink daemon calls remain bounded')
permanent_release.set()
check(bounded.drain(timeout=1), 'eventual sink returns finalize retained claims')
check(bounded.close(drain=False, timeout=0.2), 'bounded permanent sink adapter closes normally')

concurrent_received = []
concurrent = VoiceShadowAdapter(concurrent_received.append, issuer=issuer, max_queue_size=8)
barrier = threading.Barrier(8)
outcomes = []


def observe_same_input():
    barrier.wait()
    outcomes.append(concurrent.observe(segment('voice-concurrent', 'corr-concurrent', '并发', now)))


threads = [threading.Thread(target=observe_same_input) for _ in range(8)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check(sum(outcomes) == 1 and concurrent.drain(timeout=1), 'concurrent duplicate voice input is idempotent')
check(len(concurrent_received) == 1, 'only one concurrent duplicate reaches sink')

audio = MediaRef('a' * 64, 'a' * 64, 'audio', 1)
with_audio = segment('voice-media', 'corr-media', '正式文本', now, audio=audio)
check(with_audio.audio == audio, 'voice DTO carries only an immutable audio reference')
forged = copy.copy(with_audio)
check(not concurrent.observe(forged), 'voice adapter rejects DTOs not issued by its integration issuer')
try:
    replace(with_audio, text='复制')
except TypeError:
    pass
else:
    raise AssertionError('dataclass replacement cannot forge an issued DTO')
try:
    AcceptedVoiceSegment('voice-forged', 'corr-forged', '伪造', now)
except TypeError:
    pass
else:
    raise AssertionError('manual construction cannot forge an issued DTO')
concurrent.close()
print('ALL PASS test_input_voice_shadow')
