from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from guanjia.contracts import EventId
from guanjia.input.contracts import (
    CurrentProcessingResult,
    IdentityEvidence,
    InputMessage,
    MediaRef,
    ProcessorSpec,
    StateObservation,
)
from guanjia.input.store import InputStore
from guanjia.input.delivery import DeliveryQueue


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
media = MediaRef('a' * 64, 'a' * 64, 'audio', 3, {'duration': 1})
identity = IdentityEvidence('voiceprint', 'owner', 0.9, 'mic', now)
message = InputMessage(
    EventId('input-1'),
    'openclaw-1',
    'voice',
    now,
    (media,),
    (identity,),
    metadata={'nested': [{'value': 'frozen'}]},
)
check(message.media[0].metadata['duration'] == 1, 'media metadata retained')
check(message.identity_evidence[0].candidate_id == 'owner', 'identity retained')
try:
    message.source = 'wechat'
except FrozenInstanceError:
    pass
else:
    raise AssertionError('input message must be immutable')
try:
    message.metadata['nested'][0]['value'] = 'changed'
except TypeError:
    pass
else:
    raise AssertionError('nested metadata must be deeply immutable')

observation = StateObservation('presence', 'home', 'camera', now, now + timedelta(minutes=1), True, 0.9)
check(observation.valid_until > observation.observed_at, 'state validity accepted')
spec = ProcessorSpec('asr', 'v1', ('audio',), ('text',), 1, True)
check(spec.required and spec.timeout_seconds == 1, 'processor declaration retained')
with TemporaryDirectory() as temporary:
    store = InputStore(f'{temporary}/input.sqlite3')
    check(store.store_only(message), 'non-deliverable input metadata inserted')
    check(not store.store_only(message), 'non-deliverable input insert is idempotent')
    conflicting = InputMessage(EventId('input-1'), 'different-ref', 'voice', now)
    try:
        store.store_only(conflicting)
    except ValueError:
        pass
    else:
        raise AssertionError('same input ID with different content must conflict')
    result = CurrentProcessingResult(EventId('input-1'), 'asr', 'v1', {'text': 'hello'}, 0.8, now)
    store.set_current_processing_result(result)
    check(store.get_current_processing_result('input-1') == result, 'only current processing result stored')
    newer_low = CurrentProcessingResult(
        EventId('input-1'), 'asr', 'v2', {'text': 'lower'}, 0.79, now + timedelta(seconds=1)
    )
    check(not store.set_current_processing_result(newer_low, margin=0.05), 'close result does not overwrite')
    check(store.get_current_processing_result('input-1').review_required, 'close result requires review')
    try:
        store.set_current_processing_result(
            CurrentProcessingResult(EventId('input-1'), 'asr', 'v3', {'text': 'conflict'}, 0.9, now)
        )
    except ValueError:
        pass
    else:
        raise AssertionError('same timestamp divergent result must conflict')
    delivery_message = InputMessage(EventId('input-2'), 'openclaw-2', 'voice', now)
    check(store.save_and_enqueue(delivery_message, '{"input":"2"}', now), 'input and outbox atomic')
    other_store = InputStore(f'{temporary}/input.sqlite3')
    try:
        other_store.store_only(conflicting)
    except ValueError:
        pass
    else:
        raise AssertionError('separate SQLite instances preserve idempotency conflict')
    other_store.close()
    queue = DeliveryQueue(f'{temporary}/input.sqlite3')
    leased = queue.lease_next(now=now + timedelta(seconds=1))
    check(leased and leased.input_id == 'input-2', 'durable outbox is separately leasable')
    queue.close()
    store.close()
print('ALL PASS test_input_contracts')
