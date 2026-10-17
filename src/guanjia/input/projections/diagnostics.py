"""Read-only diagnostics projection for passive input adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
from typing import Protocol

from guanjia.contracts import CorrelationId, EventId
from guanjia.input.contracts import StateContext


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('captured_at must be an aware UTC datetime')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class InputDiagnosticsRecord:
    """Immutable environment snapshot associated with a global input correlation."""

    input_id: EventId
    correlation_id: CorrelationId
    captured_at: datetime
    context: StateContext

    def __post_init__(self) -> None:
        if not isinstance(self.input_id, str) or not self.input_id.strip():
            raise ValueError('input_id must be a non-empty string')
        if not isinstance(self.correlation_id, str) or not self.correlation_id.strip():
            raise ValueError('correlation_id must be a non-empty string')
        if not isinstance(self.context, StateContext):
            raise ValueError('context must be a StateContext')
        object.__setattr__(self, 'input_id', EventId(self.input_id))
        object.__setattr__(self, 'correlation_id', CorrelationId(self.correlation_id))
        object.__setattr__(self, 'captured_at', _utc(self.captured_at))


class InputDiagnosticsSink(Protocol):
    """Write-only diagnostics boundary, separate from Brain and every output channel."""

    def project(self, record: InputDiagnosticsRecord) -> None:
        """Accept one diagnostic snapshot."""


class InMemoryDiagnosticsProjection:
    """Thread-safe diagnostic test projection with idempotency by input ID."""

    def __init__(self) -> None:
        self._records: dict[str, InputDiagnosticsRecord] = {}
        self._lock = threading.RLock()

    def project(self, record: InputDiagnosticsRecord) -> None:
        with self._lock:
            current = self._records.get(str(record.input_id))
            if current is not None and current != record:
                raise ValueError(f'input_id {record.input_id} has conflicting diagnostics')
            self._records[str(record.input_id)] = record

    def records(self) -> tuple[InputDiagnosticsRecord, ...]:
        with self._lock:
            return tuple(self._records.values())
