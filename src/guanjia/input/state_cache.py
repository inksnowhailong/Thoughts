"""In-memory environmental-state cache; snapshotting never triggers collection."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .contracts import StateContext, StateObservation, StateValue


class StateCache:
    """Retains observations from all sources so conflicts stay visible."""

    def __init__(self) -> None:
        self._observations: dict[str, list[StateObservation]] = defaultdict(list)
        self._lock = threading.RLock()

    def record(self, observation: StateObservation) -> None:
        """Add an observation, replacing only the same source's older observation."""
        with self._lock:
            bucket = self._observations[observation.key]
            existing = next(
                (current for current in bucket if current.source == observation.source),
                None,
            )
            if existing is not None and existing.observed_at > observation.observed_at:
                return
            if existing is not None and existing.observed_at == observation.observed_at:
                same_value = any(
                    current.source == observation.source
                    and current.observed_at == observation.observed_at
                    and _json_key(current.value) == _json_key(observation.value)
                    for current in bucket
                )
                if not same_value:
                    bucket.append(observation)
                return
            bucket[:] = [current for current in bucket if current.source != observation.source]
            bucket.append(observation)

    def observations(self, key: str) -> tuple[StateObservation, ...]:
        with self._lock:
            return tuple(self._observations.get(key, ()))

    def snapshot(self, *, now: datetime) -> StateContext:
        """Read cached state only; callers must schedule collection elsewhere."""
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError('now must be an aware UTC datetime')
        with self._lock:
            entries = {key: tuple(value) for key, value in self._observations.items()}
        values = {key: self._aggregate(key, value, now) for key, value in entries.items()}
        return StateContext(captured_at=now, values=values)

    @staticmethod
    def _aggregate(
        key: str, observations: Iterable[StateObservation], now: datetime
    ) -> StateValue:
        all_entries = tuple(observations)
        healthy = tuple(entry for entry in all_entries if entry.healthy)
        fresh = tuple(entry for entry in healthy if entry.valid_until > now)
        if not healthy:
            return StateValue(key, None, 'unknown', all_entries, 'all sources unhealthy')
        if not fresh:
            return StateValue(key, None, 'stale', all_entries, 'no healthy fresh observation')

        distinct = {_json_key(entry.value) for entry in fresh}
        if len(distinct) > 1:
            return StateValue(
                key,
                None,
                'uncertain',
                all_entries,
                f'{len(distinct)} conflicting fresh values',
            )
        selected = max(fresh, key=lambda item: (item.confidence, item.observed_at))
        return StateValue(key, selected.value, 'fresh', all_entries)


def _json_key(value: object) -> str:
    """Render frozen JSON containers into a stable comparable form."""
    if isinstance(value, Mapping):
        value = {key: _json_value(item) for key, item in value.items()}
    else:
        value = _json_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value
