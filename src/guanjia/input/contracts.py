"""Immutable contracts owned by the input domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from types import MappingProxyType
from typing import Mapping

from guanjia.contracts import EventId, FrozenJsonValue, JsonValue


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f'{field_name} must be an aware UTC datetime')
    if value.utcoffset() != timedelta(0):
        raise ValueError(f'{field_name} must be UTC')
    return value.astimezone(timezone.utc)


def _freeze_json(value: object) -> FrozenJsonValue:
    """Validate a JSON value and return a recursively immutable representation."""
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
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, list) or isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError('value must contain only JSON values')


def _frozen_mapping(value: Mapping[str, JsonValue] | None) -> Mapping[str, FrozenJsonValue]:
    frozen = _freeze_json(dict(value or {}))
    assert isinstance(frozen, Mapping)
    return frozen


def _non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field_name} must be a non-empty string')
    return value


def _event_id(value: EventId, field_name: str) -> EventId:
    if not isinstance(value, str):
        raise ValueError(f'{field_name} must be an EventId string')
    _non_empty(value, field_name)
    return EventId(value)


def _confidence(value: float, field_name: str = 'confidence') -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(f'{field_name} must be between 0 and 1')
    return float(value)


@dataclass(frozen=True)
class MediaRef:
    """Opaque reference to media held by the input media store."""

    media_id: str
    sha256: str
    media_type: str
    size_bytes: int
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _non_empty(self.media_id, 'media_id')
        if len(self.sha256) != 64 or any(char not in '0123456789abcdef' for char in self.sha256):
            raise ValueError('sha256 must be a lowercase SHA-256 digest')
        if self.media_type not in {'audio', 'image', 'video', 'attachment'}:
            raise ValueError('media_type must be audio, image, video, or attachment')
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError('size_bytes must be a non-negative integer')
        object.__setattr__(self, 'metadata', _frozen_mapping(self.metadata))
        if self.expires_at is not None:
            object.__setattr__(self, 'expires_at', _utc(self.expires_at, 'expires_at'))


@dataclass(frozen=True)
class IdentityEvidence:
    """A candidate identity signal, deliberately not a final identity decision."""

    evidence_type: str
    candidate_id: str
    confidence: float
    source: str
    observed_at: datetime
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ('evidence_type', 'candidate_id', 'source'):
            _non_empty(getattr(self, name), name)
        object.__setattr__(self, 'confidence', _confidence(self.confidence))
        object.__setattr__(self, 'observed_at', _utc(self.observed_at, 'observed_at'))
        object.__setattr__(self, 'metadata', _frozen_mapping(self.metadata))


@dataclass(frozen=True)
class CurrentProcessingResult:
    """The only processing result retained for an input."""

    input_id: EventId
    processor_name: str
    processor_version: str
    fields: Mapping[str, FrozenJsonValue]
    confidence: float
    processed_at: datetime
    review_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'input_id', _event_id(self.input_id, 'input_id'))
        _non_empty(self.processor_name, 'processor_name')
        _non_empty(self.processor_version, 'processor_version')
        object.__setattr__(self, 'fields', _frozen_mapping(self.fields))
        object.__setattr__(self, 'confidence', _confidence(self.confidence))
        object.__setattr__(self, 'processed_at', _utc(self.processed_at, 'processed_at'))
        if not isinstance(self.review_required, bool):
            raise ValueError('review_required must be a bool')


@dataclass(frozen=True)
class InputMessage:
    """Complete input delivered to the core exactly by its global input ID."""

    input_id: EventId
    message_ref: str
    source: str
    received_at: datetime
    media: tuple[MediaRef, ...] = ()
    identity_evidence: tuple[IdentityEvidence, ...] = ()
    processing_result: CurrentProcessingResult | None = None
    state_context_ref: str | None = None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'input_id', _event_id(self.input_id, 'input_id'))
        _non_empty(self.message_ref, 'message_ref')
        _non_empty(self.source, 'source')
        object.__setattr__(self, 'received_at', _utc(self.received_at, 'received_at'))
        media = tuple(self.media)
        evidence = tuple(self.identity_evidence)
        if not all(isinstance(item, MediaRef) for item in media):
            raise ValueError('media must contain MediaRef values')
        if not all(isinstance(item, IdentityEvidence) for item in evidence):
            raise ValueError('identity_evidence must contain IdentityEvidence values')
        object.__setattr__(self, 'media', media)
        object.__setattr__(self, 'identity_evidence', evidence)
        if self.processing_result is not None and self.processing_result.input_id != self.input_id:
            raise ValueError('processing_result input_id must match input_id')
        if self.state_context_ref is not None:
            _non_empty(self.state_context_ref, 'state_context_ref')
        object.__setattr__(self, 'metadata', _frozen_mapping(self.metadata))


@dataclass(frozen=True)
class StateObservation:
    """One source observation; observations are retained even when conflicting."""

    key: str
    value: FrozenJsonValue
    source: str
    observed_at: datetime
    valid_until: datetime
    healthy: bool
    confidence: float
    error: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.key, 'key')
        _non_empty(self.source, 'source')
        observed_at = _utc(self.observed_at, 'observed_at')
        valid_until = _utc(self.valid_until, 'valid_until')
        if valid_until < observed_at:
            raise ValueError('valid_until cannot precede observed_at')
        object.__setattr__(self, 'observed_at', observed_at)
        object.__setattr__(self, 'valid_until', valid_until)
        object.__setattr__(self, 'confidence', _confidence(self.confidence))
        object.__setattr__(self, 'value', _freeze_json(self.value))
        if not isinstance(self.healthy, bool):
            raise ValueError('healthy must be a bool')
        if self.error is not None:
            _non_empty(self.error, 'error')


@dataclass(frozen=True)
class StateValue:
    """Aggregated state with explicit freshness rather than an implied fact."""

    key: str
    value: FrozenJsonValue | None
    status: str
    observations: tuple[StateObservation, ...]
    conflict_summary: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.key, 'key')
        if self.status not in {'fresh', 'stale', 'unknown', 'uncertain'}:
            raise ValueError('unsupported state status')
        observations = tuple(self.observations)
        if not all(isinstance(observation, StateObservation) for observation in observations):
            raise ValueError('observations must contain StateObservation values')
        if self.value is not None:
            object.__setattr__(self, 'value', _freeze_json(self.value))
        object.__setattr__(self, 'observations', observations)
        if self.conflict_summary is not None:
            _non_empty(self.conflict_summary, 'conflict_summary')


@dataclass(frozen=True)
class StateContext:
    """A snapshot assembled from cached observations without collecting anew."""

    captured_at: datetime
    values: Mapping[str, StateValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'captured_at', _utc(self.captured_at, 'captured_at'))
        values = dict(self.values)
        if any(not isinstance(key, str) or not isinstance(value, StateValue) for key, value in values.items()):
            raise ValueError('values must map string keys to StateValue')
        object.__setattr__(self, 'values', MappingProxyType(values))


@dataclass(frozen=True)
class ProcessorSpec:
    """Declarative processor contract independent of deployment topology."""

    name: str
    version: str
    input_types: tuple[str, ...]
    output_fields: tuple[str, ...]
    timeout_seconds: float
    required: bool
    fallback_names: tuple[str, ...] = ()
    resources: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    calibrator_name: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.name, 'name')
        _non_empty(self.version, 'version')
        if not self.input_types or not all(isinstance(item, str) and item.strip() for item in self.input_types):
            raise ValueError('input_types must not be empty')
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be positive')
        if not isinstance(self.required, bool):
            raise ValueError('required must be a bool')
        if not all(isinstance(item, str) and item.strip() for item in self.output_fields):
            raise ValueError('output_fields must contain non-empty strings')
        if not all(isinstance(item, str) and item.strip() for item in self.fallback_names):
            raise ValueError('fallback_names must contain non-empty strings')
        if self.calibrator_name is not None:
            _non_empty(self.calibrator_name, 'calibrator_name')
        object.__setattr__(self, 'input_types', tuple(self.input_types))
        object.__setattr__(self, 'output_fields', tuple(self.output_fields))
        object.__setattr__(self, 'fallback_names', tuple(self.fallback_names))
        object.__setattr__(self, 'resources', _frozen_mapping(self.resources))


@dataclass(frozen=True)
class ProcessorResult:
    """Raw processor outcome; confidence is calibrated before persistence."""

    processor_name: str
    processor_version: str
    fields: Mapping[str, FrozenJsonValue]
    raw_confidence: float
    completed_at: datetime
    error: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.processor_name, 'processor_name')
        _non_empty(self.processor_version, 'processor_version')
        object.__setattr__(self, 'fields', _frozen_mapping(self.fields))
        object.__setattr__(self, 'raw_confidence', _confidence(self.raw_confidence, 'raw_confidence'))
        object.__setattr__(self, 'completed_at', _utc(self.completed_at, 'completed_at'))
        if self.error is not None:
            _non_empty(self.error, 'error')


@dataclass(frozen=True)
class FailedInput:
    """Terminal failure after required processing or delivery cannot complete."""

    input_id: EventId
    stage: str
    reason: str
    attempts: int
    last_attempt_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'input_id', _event_id(self.input_id, 'input_id'))
        _non_empty(self.stage, 'stage')
        _non_empty(self.reason, 'reason')
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) or self.attempts < 1:
            raise ValueError('attempts must be a positive integer')
        object.__setattr__(self, 'last_attempt_at', _utc(self.last_attempt_at, 'last_attempt_at'))
