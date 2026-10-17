"""Passive environment adapter: injected reads only, never a collector trigger."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
from typing import Callable

from guanjia.contracts import CorrelationId, EventId, JsonValue
from guanjia.input.contracts import StateContext, StateObservation
from guanjia.input.projections.diagnostics import InputDiagnosticsRecord, InputDiagnosticsSink
from guanjia.input.state_cache import StateCache

from ._async_shadow import BoundedShadowAdapter, IdempotencyStore, ShadowMetrics
from .contracts import ShadowInputRecord

EnvironmentReader = Callable[[], JsonValue]


@dataclass(frozen=True)
class EnvironmentReaders:
    """Dependency-injected cached-state readers; none may perform collection."""

    program_context: EnvironmentReader
    program_quiet: EnvironmentReader
    runtime_state: EnvironmentReader
    service_health: EnvironmentReader


class EnvironmentShadowAdapter:
    """Copies state in a background worker; its bounded local cache is best-effort only.

    Once an in-memory completed entry is evicted, durable projection idempotency is
    responsible for suppressing any duplicate diagnostic effect.
    """

    def __init__(
        self,
        cache: StateCache,
        readers: EnvironmentReaders,
        projection: InputDiagnosticsSink,
        *,
        ttl: timedelta = timedelta(seconds=15),
        max_observed_inputs: int = 4096,
    ) -> None:
        if not isinstance(ttl, timedelta) or ttl.total_seconds() < 0:
            raise ValueError('ttl must be a non-negative timedelta')
        if (
            not isinstance(max_observed_inputs, int)
            or isinstance(max_observed_inputs, bool)
            or max_observed_inputs < 1
        ):
            raise ValueError('max_observed_inputs must be a positive integer')
        self._cache = cache
        self._readers = readers
        self._projection = projection
        self._ttl = ttl
        self._max_observed_inputs = max_observed_inputs
        self._condition = threading.Condition(threading.RLock())
        self._observed: OrderedDict[str, tuple[str, StateContext] | None] = OrderedDict()

    def collect_in_background(self, input_id: EventId, correlation_id: CorrelationId, *, now: datetime) -> StateContext:
        """Blocking background-only collection; source threads must use EnvironmentShadowAsyncAdapter."""
        key = self._identifier(input_id, 'input_id')
        correlation = self._identifier(correlation_id, 'correlation_id')
        captured_at = self._utc(now)
        with self._condition:
            while key in self._observed and self._observed[key] is None:
                self._condition.wait()
            existing = self._observed.get(key)
            if existing is not None:
                if existing[0] != correlation:
                    raise ValueError(f'input_id {input_id} has a different correlation_id')
                self._observed.move_to_end(key)
                return existing[1]
            self._observed[key] = None
        try:
            observations = tuple(
                self._read_observation(name, reader, captured_at) for name, reader in self._reader_items()
            )
            staged = StateCache()
            for observation in observations:
                staged.record(observation)
            projected_context = staged.snapshot(now=captured_at)
            self._projection.project(
                InputDiagnosticsRecord(EventId(key), CorrelationId(correlation), captured_at, projected_context)
            )
            for observation in observations:
                self._cache.record(observation)
            context = self._cache.snapshot(now=captured_at)
        except Exception:
            with self._condition:
                self._observed.pop(key, None)
                self._condition.notify_all()
            raise
        with self._condition:
            self._observed[key] = (correlation, context)
            self._observed.move_to_end(key)
            self._trim_observed()
            self._condition.notify_all()
            return context

    def _reader_items(self) -> tuple[tuple[str, EnvironmentReader], ...]:
        return (
            ('program_context', self._readers.program_context),
            ('program_quiet', self._readers.program_quiet),
            ('runtime_state', self._readers.runtime_state),
            ('service_health', self._readers.service_health),
        )

    def _read_observation(self, key: str, reader: EnvironmentReader, now: datetime) -> StateObservation:
        try:
            value = reader()
        except Exception as error:  # A failed passive read is state, not a source-pipeline failure.
            return StateObservation(key, None, key, now, now, False, 0, f'{type(error).__name__}: <redacted>')
        return StateObservation(key, value, key, now, now + self._ttl, True, 1)

    @staticmethod
    def _identifier(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{field_name} must be a non-empty string')
        return value

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError('now must be an aware UTC datetime')
        return value.astimezone(timezone.utc)

    def _trim_observed(self) -> None:
        """Bound completed process-local idempotency entries without evicting active requests."""
        while len(self._observed) > self._max_observed_inputs:
            oldest_key, oldest = next(iter(self._observed.items()))
            if oldest is None:
                self._observed.move_to_end(oldest_key)
                if all(value is None for value in self._observed.values()):
                    return
                continue
            self._observed.popitem(last=False)


class EnvironmentShadowAsyncAdapter:
    """The only source-thread entry point when injected readers may block."""

    def __init__(
        self,
        adapter: EnvironmentShadowAdapter,
        *,
        max_queue_size: int = 64,
        sink_timeout: float = 1.0,
        idempotency_store: IdempotencyStore | None = None,
        max_outstanding_sink_calls: int = 4,
    ) -> None:
        self._adapter = adapter
        self._delivery = BoundedShadowAdapter(
            self._run,
            max_queue_size=max_queue_size,
            sink_timeout=sink_timeout,
            idempotency_store=idempotency_store,
            max_outstanding_sink_calls=max_outstanding_sink_calls,
        )

    def observe(self, input_id: EventId, correlation_id: CorrelationId, *, now: datetime) -> bool:
        """Queue a background observation without invoking a reader on the source thread."""
        captured_at = EnvironmentShadowAdapter._utc(now)
        return self._delivery.observe(
            ShadowInputRecord(input_id, correlation_id, 'environment', 'environment-observation', captured_at)
        )

    def drain(self, *, timeout: float | None = None) -> bool:
        return self._delivery.drain(timeout=timeout)

    def close(self, *, drain: bool = True, timeout: float | None = None) -> bool:
        return self._delivery.close(drain=drain, timeout=timeout)

    def metrics(self) -> ShadowMetrics:
        return self._delivery.metrics()

    def _run(self, record: ShadowInputRecord) -> None:
        self._adapter.collect_in_background(record.input_id, record.correlation_id, now=record.received_at)
