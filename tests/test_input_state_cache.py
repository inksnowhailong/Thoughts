from datetime import datetime, timedelta, timezone

from guanjia.input.contracts import StateObservation
from guanjia.input.state_cache import StateCache


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
cache = StateCache()
cache.record(
    StateObservation(
        'presence', {'location': ['home']}, 'camera', now, now + timedelta(minutes=1), True, 0.9
    )
)
check(cache.snapshot(now=now).values['presence'].status == 'fresh', 'fresh state is usable')
cache.record(StateObservation('presence', {'location': ['away']}, 'wifi', now, now + timedelta(minutes=1), True, 0.8))
conflict = cache.snapshot(now=now).values['presence']
check(conflict.status == 'uncertain' and len(conflict.observations) == 2, 'conflict is retained')
cache = StateCache()
cache.record(StateObservation('app', 'game', 'probe', now, now, True, 0.8))
check(cache.snapshot(now=now + timedelta(seconds=1)).values['app'].status == 'stale', 'expired state is stale')
cache = StateCache()
cache.record(StateObservation('mic', None, 'probe', now, now, False, 0, 'failed'))
check(cache.snapshot(now=now).values['mic'].status == 'unknown', 'unhealthy state is unknown')
print('ALL PASS test_input_state_cache')
