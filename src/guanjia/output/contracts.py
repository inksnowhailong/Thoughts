"""输出模块的不可变领域契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import math


class OutputStatus(str, Enum):
    WAITING = 'waiting'
    SENT = 'sent'
    DELIVERED = 'delivered'
    FAILED = 'failed'
    FALLBACK = 'fallback'


class DeliveryRequirement(str, Enum):
    BEST_EFFORT = 'best_effort'
    MUST_BE_SEEN = 'must_be_seen'


class Importance(str, Enum):
    LOW = 'low'
    NORMAL = 'normal'
    HIGH = 'high'
    CRITICAL = 'critical'


@dataclass(frozen=True)
class OutputAuthorizationContext:
    actor_id: str
    task_id: str | None
    intent: str
    capability: str
    object_scope: str
    risk_scope: str
    provenance_summary: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.actor_id, 'actor_id'), (self.intent, 'intent'),
            (self.capability, 'capability'), (self.object_scope, 'object_scope'),
            (self.risk_scope, 'risk_scope'), (self.provenance_summary, 'provenance_summary'),
        ):
            _required(value, name)
        if self.task_id is not None:
            _required(self.task_id, 'task_id')


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')


def _utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{name} must be an aware UTC datetime')


def _unique_channels(channels: tuple[str, ...], name: str) -> tuple[str, ...]:
    if type(channels) is not tuple:
        raise ValueError(f'{name} channels must be a tuple')
    result: list[str] = []
    for channel in channels:
        _required(channel, f'{name} channel')
        if channel not in result:
            result.append(channel)
    return tuple(result)


@dataclass(frozen=True)
class Expression:
    tone: str = 'neutral'
    emotion_intensity: float = 0.0
    rate: float = 1.0
    pause_after_ms: int = 0
    interruptible: bool = True
    suitable_for_speech: bool = True

    def __post_init__(self) -> None:
        _required(self.tone, 'tone')
        if type(self.emotion_intensity) not in (int, float) or not math.isfinite(self.emotion_intensity) or not 0 <= self.emotion_intensity <= 1:
            raise ValueError('emotion_intensity must be between 0 and 1')
        if type(self.rate) not in (int, float) or not math.isfinite(self.rate) or not 0.5 <= self.rate <= 2.0:
            raise ValueError('rate must be between 0.5 and 2.0')
        if type(self.pause_after_ms) is not int or self.pause_after_ms < 0:
            raise ValueError('pause_after_ms must be a non-negative integer')


@dataclass(frozen=True)
class OutputRequest:
    output_id: str
    dedupe_key: str
    actor_id: str
    conversation_id: str
    text: str | None
    artifact_refs: tuple[str, ...]
    preferred_channels: tuple[str, ...]
    fallback_channels: tuple[str, ...] = ()
    importance: Importance = Importance.NORMAL
    delivery_requirement: DeliveryRequirement = DeliveryRequirement.BEST_EFFORT
    authorization: OutputAuthorizationContext | None = None
    ack_timeout_seconds: float | None = None
    task_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.output_id, 'output_id'), (self.dedupe_key, 'dedupe_key'),
            (self.actor_id, 'actor_id'), (self.conversation_id, 'conversation_id'),
        ):
            _required(value, name)
        if self.text is not None and (not isinstance(self.text, str) or not self.text.strip()):
            raise ValueError('text must be None or a non-empty string')
        if type(self.artifact_refs) is not tuple:
            raise ValueError('artifact_refs must be a tuple')
        if not self.text and not self.artifact_refs:
            raise ValueError('text or artifact_refs is required')
        refs: list[str] = []
        for reference in self.artifact_refs:
            _required(reference, 'artifact reference')
            if reference not in refs:
                refs.append(reference)
        preferred = _unique_channels(self.preferred_channels, 'preferred')
        fallback = tuple(channel for channel in _unique_channels(self.fallback_channels, 'fallback') if channel not in preferred)
        if not preferred:
            raise ValueError('preferred_channels must not be empty')
        if not isinstance(self.importance, Importance):
            raise ValueError('importance must be an Importance')
        if not isinstance(self.delivery_requirement, DeliveryRequirement):
            raise ValueError('delivery_requirement must be a DeliveryRequirement')
        if not isinstance(self.authorization, OutputAuthorizationContext):
            raise ValueError('authorization is required')
        if self.authorization.actor_id != self.actor_id:
            raise ValueError('authorization actor must match output actor')
        if self.authorization.task_id != self.task_id:
            raise ValueError('authorization task must match output task')
        if self.task_id is not None:
            _required(self.task_id, 'task_id')
        if self.delivery_requirement is DeliveryRequirement.MUST_BE_SEEN:
            if type(self.ack_timeout_seconds) not in (int, float) or not math.isfinite(self.ack_timeout_seconds) or self.ack_timeout_seconds <= 0:
                raise ValueError('must_be_seen requires positive ack_timeout_seconds')
        elif self.ack_timeout_seconds is not None:
            raise ValueError('ack_timeout_seconds only applies to must_be_seen')
        object.__setattr__(self, 'preferred_channels', preferred)
        object.__setattr__(self, 'fallback_channels', fallback)
        object.__setattr__(self, 'artifact_refs', tuple(refs))

    def semantic_fingerprint(self) -> tuple[object, ...]:
        return (
            self.actor_id, self.conversation_id, self.text, self.artifact_refs,
            self.preferred_channels, self.fallback_channels, self.importance,
            self.delivery_requirement,
            self.authorization, self.ack_timeout_seconds,
            self.task_id,
        )


@dataclass(frozen=True)
class DeliveryAttempt:
    output_id: str
    channel: str
    status: OutputStatus
    attempted_at: datetime
    delivery_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _required(self.output_id, 'output_id')
        _required(self.channel, 'channel')
        _utc(self.attempted_at, 'attempted_at')
        if type(self.status) is not OutputStatus or self.status not in (OutputStatus.SENT, OutputStatus.FAILED):
            raise ValueError('attempt status must be sent or failed')
        if self.status is OutputStatus.SENT:
            if not self.delivery_id or self.error is not None:
                raise ValueError('sent attempt requires delivery_id and no error')
        elif self.delivery_id is not None or not self.error:
            raise ValueError('failed attempt requires error and no delivery_id')


@dataclass(frozen=True)
class Receipt:
    output_id: str
    dedupe_key: str
    status: OutputStatus
    channel: str | None = None
    delivery_id: str | None = None
    failure_reason: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    version: int = 0
    claim_channel: str | None = None
    claim_phase: str | None = None
    claim_operation_id: str | None = None
    claim_owner: str | None = None
    claim_until: datetime | None = None
    claim_fencing_token: int | None = None

    def __post_init__(self) -> None:
        _required(self.output_id, 'output_id')
        _required(self.dedupe_key, 'dedupe_key')
        if type(self.status) is not OutputStatus:
            raise ValueError('status must be an OutputStatus')
        if type(self.version) is not int or self.version < 0:
            raise ValueError('receipt version must be a non-negative integer')
        claim_values = (
            self.claim_channel, self.claim_phase, self.claim_operation_id,
            self.claim_owner, self.claim_until, self.claim_fencing_token,
        )
        if any(value is not None for value in claim_values):
            if not all(value is not None for value in claim_values):
                raise ValueError('receipt claim fields must be present together')
            _required(self.claim_channel, 'claim_channel')
            _required(self.claim_phase, 'claim_phase')
            _required(self.claim_operation_id, 'claim_operation_id')
            _required(self.claim_owner, 'claim_owner')
            _utc(self.claim_until, 'claim_until')
            if type(self.claim_fencing_token) is not int or self.claim_fencing_token < 1:
                raise ValueError('claim_fencing_token must be a positive integer')
        if self.status is OutputStatus.WAITING:
            if self.channel is not None or self.delivery_id is not None or self.failure_reason is not None or self.sent_at is not None or self.delivered_at is not None:
                raise ValueError('waiting receipt cannot contain delivery data')
        elif self.status is OutputStatus.FAILED:
            if self.channel is not None or self.delivery_id is not None or not self.failure_reason or self.sent_at is not None or self.delivered_at is not None:
                raise ValueError('failed receipt requires only failure_reason')
        else:
            if not self.channel or not self.delivery_id or self.failure_reason is not None or self.sent_at is None:
                raise ValueError('successful receipt requires channel and delivery_id')
            _utc(self.sent_at, 'sent_at')
            if self.status is OutputStatus.DELIVERED:
                if self.delivered_at is None:
                    raise ValueError('delivered receipt requires delivered_at')
                _utc(self.delivered_at, 'delivered_at')
            elif self.delivered_at is not None:
                raise ValueError('undelivered receipt cannot contain delivered_at')


class IdempotencyConflict(ValueError):
    """同一去重键表示不同语义输出时拒绝覆盖。"""
