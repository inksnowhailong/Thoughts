"""Bounded, daemonized delivery for passive input shadows."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Protocol

from .contracts import InputShadowSink, ShadowInputRecord

ShadowSink = Callable[[ShadowInputRecord], None] | InputShadowSink
_GLOBAL_MAX_OUTSTANDING_SINK_CALLS = 4
_GLOBAL_SINK_SLOTS = threading.BoundedSemaphore(_GLOBAL_MAX_OUTSTANDING_SINK_CALLS)


class IdempotencyStore(Protocol):
    """Process-external implementations may retain claims across restarts."""

    def claim(self, input_id: str) -> bool:
        """Atomically transition an absent ID to pending."""

    def begin(self, input_id: str) -> None:
        """Transition a pending ID to in-flight."""

    def complete(self, input_id: str) -> None:
        """Transition a successful ID to completed."""

    def release(self, input_id: str) -> None:
        """Remove a pending or in-flight ID so the input can be retried."""


class ProcessIdempotencyStore:
    """Thread-safe process-only state machine; only completed IDs are capacity-evicted."""

    def __init__(self, *, max_entries: int = 4096) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError('max_entries must be a positive integer')
        self._max_entries = max_entries
        self._claims: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.RLock()

    def claim(self, input_id: str) -> bool:
        with self._lock:
            if input_id in self._claims:
                return False
            self._claims[input_id] = 'pending'
            return True

    def begin(self, input_id: str) -> None:
        with self._lock:
            if self._claims.get(input_id) != 'pending':
                raise ValueError(f'input_id {input_id} is not pending')
            self._claims[input_id] = 'inflight'

    def complete(self, input_id: str) -> None:
        with self._lock:
            if self._claims.get(input_id) not in {'pending', 'inflight'}:
                raise ValueError(f'input_id {input_id} is not claimable')
            self._claims[input_id] = 'completed'
            self._claims.move_to_end(input_id)
            self._trim_completed()

    def release(self, input_id: str) -> None:
        with self._lock:
            self._claims.pop(input_id, None)

    def _trim_completed(self) -> None:
        completed = sum(1 for state in self._claims.values() if state == 'completed')
        while completed > self._max_entries:
            for key, state in self._claims.items():
                if state == 'completed':
                    del self._claims[key]
                    completed -= 1
                    break


@dataclass(frozen=True)
class ShadowMetrics:
    """Loss and isolation counters for one adapter instance."""

    accepted: int
    queue_full: int
    dropped_on_close: int
    duplicates: int
    sink_failures: int
    delivered: int
    circuit_open: int
    timed_out: int

    @property
    def dropped_queue_full(self) -> int:
        """Compatibility alias for the explicit queue_full counter."""
        return self.queue_full


class BoundedShadowAdapter:
    """Linearizable producer with a bounded queue and daemon-isolated sink calls."""

    def __init__(
        self,
        sink: ShadowSink,
        *,
        max_queue_size: int,
        sink_timeout: float = 1.0,
        idempotency_store: IdempotencyStore | None = None,
        max_outstanding_sink_calls: int = 4,
    ) -> None:
        if not isinstance(max_queue_size, int) or isinstance(max_queue_size, bool) or max_queue_size < 1:
            raise ValueError('max_queue_size must be a positive integer')
        self._validate_timeout(sink_timeout, field_name='sink_timeout', positive=True)
        if (
            not isinstance(max_outstanding_sink_calls, int)
            or isinstance(max_outstanding_sink_calls, bool)
            or max_outstanding_sink_calls < 1
        ):
            raise ValueError('max_outstanding_sink_calls must be a positive integer')
        self._sink = sink
        self._max_queue_size = max_queue_size
        self._sink_timeout = sink_timeout
        self._condition = threading.Condition(threading.RLock())
        self._closed = False
        self._drain_on_close = True
        self._pending: deque[ShadowInputRecord] = deque()
        self._inflight: dict[str, ShadowInputRecord] = {}
        self._abort_inflight = False
        self._idempotency = idempotency_store or ProcessIdempotencyStore()
        self._accepted = 0
        self._queue_full = 0
        self._dropped_on_close = 0
        self._duplicates = 0
        self._sink_failures = 0
        self._delivered = 0
        self._circuit_open = 0
        self._timed_out = 0
        self._worker = threading.Thread(target=self._run, name=f'{type(self).__name__}-shadow', daemon=True)
        self._worker.start()

    def observe(self, record: ShadowInputRecord) -> bool:
        """Enqueue without waiting; false means closed, duplicate, or intentionally dropped."""
        with self._condition:
            if self._closed:
                return False
            input_id = str(record.input_id)
            if not self._idempotency.claim(input_id):
                self._duplicates += 1
                return False
            if len(self._pending) >= self._max_queue_size:
                self._idempotency.release(input_id)
                self._queue_full += 1
                return False
            self._pending.append(record)
            self._accepted += 1
            self._condition.notify()
            return True

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait deterministically for pending and in-flight accepted records."""
        wait_seconds = 5.0 if timeout is None else self._validate_timeout(timeout, field_name='timeout')
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while self._pending or self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, *, drain: bool = True, timeout: float | None = None) -> bool:
        """Linearize shutdown; non-draining close abandons pending and isolated sink calls."""
        wait_seconds = 5.0 if timeout is None else self._validate_timeout(timeout, field_name='timeout')
        with self._condition:
            if self._closed:
                already_closed = True
            else:
                already_closed = False
                self._closed = True
                self._drain_on_close = drain
                if not drain:
                    self._dropped_on_close += len(self._pending)
                    for record in self._pending:
                        self._idempotency.release(str(record.input_id))
                    self._pending.clear()
                    self._abort_inflight = True
                self._condition.notify_all()
        if already_closed:
            return self._join(wait_seconds)
        if drain and not self.drain(timeout=wait_seconds):
            return False
        return self._join(wait_seconds)

    def metrics(self) -> ShadowMetrics:
        """Return a stable snapshot of delivery counters."""
        with self._condition:
            return ShadowMetrics(
                accepted=self._accepted,
                queue_full=self._queue_full,
                dropped_on_close=self._dropped_on_close,
                duplicates=self._duplicates,
                sink_failures=self._sink_failures,
                delivered=self._delivered,
                circuit_open=self._circuit_open,
                timed_out=self._timed_out,
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    if self._closed:
                        return
                    self._condition.wait()
                if self._closed and not self._drain_on_close:
                    return
                item = self._pending.popleft()
                self._idempotency.begin(str(item.input_id))
                self._inflight[str(item.input_id)] = item
            outcome = self._deliver(item)
            with self._condition:
                if outcome == 'circuit':
                    self._inflight.pop(str(item.input_id), None)
                    self._idempotency.release(str(item.input_id))
                    self._sink_failures += 1
                    self._circuit_open += 1
                elif outcome == 'timeout':
                    self._timed_out += 1
                self._condition.notify_all()

    def _deliver(self, record: ShadowInputRecord) -> str:
        if not _GLOBAL_SINK_SLOTS.acquire(blocking=False):
            return 'circuit'

        def invoke() -> None:
            try:
                if callable(self._sink):
                    self._sink(record)
                else:
                    self._sink.publish(record)
            except Exception:  # Sink failures must never escape into the source pipeline.
                self._finalize_invoke(record, succeeded=False)
            else:
                self._finalize_invoke(record, succeeded=True)
            finally:
                _GLOBAL_SINK_SLOTS.release()

        threading.Thread(target=invoke, name=f'{type(self).__name__}-sink-call', daemon=True).start()
        deadline = time.monotonic() + self._sink_timeout
        while time.monotonic() < deadline:
            with self._condition:
                if self._abort_inflight:
                    return 'detached'
                if str(record.input_id) not in self._inflight:
                    return 'finished'
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        with self._condition:
            return 'finished' if str(record.input_id) not in self._inflight else 'timeout'

    def _finalize_invoke(self, record: ShadowInputRecord, *, succeeded: bool) -> None:
        """Own the claim until the actual daemon call returns, even after worker timeout."""
        input_id = str(record.input_id)
        with self._condition:
            if self._inflight.pop(input_id, None) is None:
                return
            if succeeded:
                self._idempotency.complete(input_id)
                self._delivered += 1
            else:
                self._idempotency.release(input_id)
                self._sink_failures += 1
            self._condition.notify_all()

    def _join(self, timeout: float) -> bool:
        self._worker.join(timeout)
        return not self._worker.is_alive()

    @staticmethod
    def _validate_timeout(value: float, *, field_name: str, positive: bool = False) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f'{field_name} must be a finite number')
        if value < 0 or (positive and value == 0):
            comparator = 'positive' if positive else 'non-negative'
            raise ValueError(f'{field_name} must be {comparator}')
        return float(value)
