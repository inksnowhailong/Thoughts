"""Immutable, outbound-free shadow DTOs for input edge adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from types import MappingProxyType
from typing import Mapping, Protocol

from guanjia.contracts import CorrelationId, EventId, FrozenJsonValue, JsonValue


def _non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field_name} must be a non-empty string')
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{field_name} must be an aware UTC datetime')
    return value.astimezone(timezone.utc)


def freeze_json(value: object) -> FrozenJsonValue:
    """Return a recursively immutable JSON-compatible value."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('JSON floats must be finite')
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('JSON object keys must be strings')
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item) for item in value)
    raise ValueError('value must contain only JSON values')


def frozen_mapping(value: Mapping[str, JsonValue] | None) -> Mapping[str, FrozenJsonValue]:
    frozen = freeze_json(dict(value or {}))
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True)
class ShadowInputRecord:
    """A one-way input shadow record; it has no Brain, TTS, tool, or output handle."""

    input_id: EventId
    correlation_id: CorrelationId
    source: str
    text: str
    received_at: datetime
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'input_id', EventId(_non_empty(self.input_id, 'input_id')))
        object.__setattr__(self, 'correlation_id', CorrelationId(_non_empty(self.correlation_id, 'correlation_id')))
        _non_empty(self.source, 'source')
        _non_empty(self.text, 'text')
        object.__setattr__(self, 'received_at', _utc(self.received_at, 'received_at'))
        object.__setattr__(self, 'metadata', frozen_mapping(self.metadata))


class InputShadowSink(Protocol):
    """Write-only shadow boundary. Deliberately excludes all production outputs."""

    def publish(self, record: ShadowInputRecord) -> None:
        """Persist or inspect one shadow record."""
