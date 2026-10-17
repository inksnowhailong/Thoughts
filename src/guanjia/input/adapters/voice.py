"""Passive shadow observer for already-accepted, complete voice segments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping
from weakref import WeakSet

from guanjia.contracts import CorrelationId, EventId, FrozenJsonValue
from guanjia.input.contracts import MediaRef

from ._async_shadow import BoundedShadowAdapter, IdempotencyStore, ShadowMetrics, ShadowSink
from .contracts import ShadowInputRecord, frozen_mapping


@dataclass(frozen=True, init=False, eq=False)
class AcceptedVoiceSegment:
    """The formal post-gate DTO. Pre-VAD, partial ASR, and rejected speech cannot use it."""

    input_id: EventId
    correlation_id: CorrelationId
    text: str
    accepted_at: datetime
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    audio: MediaRef | None = None

    @classmethod
    def _create(
        cls,
        input_id: EventId,
        correlation_id: CorrelationId,
        text: str,
        accepted_at: datetime,
        metadata: Mapping[str, FrozenJsonValue] | None,
        audio: MediaRef | None,
    ) -> 'AcceptedVoiceSegment':
        """Private factory used only by AcceptedInputIssuer."""
        if audio is not None and not isinstance(audio, MediaRef):
            raise ValueError('audio must be a MediaRef or None')
        record = ShadowInputRecord(
            input_id, correlation_id, 'voice', text, accepted_at, metadata or {},
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, 'input_id', record.input_id)
        object.__setattr__(instance, 'correlation_id', record.correlation_id)
        object.__setattr__(instance, 'text', record.text)
        object.__setattr__(instance, 'accepted_at', record.received_at)
        object.__setattr__(instance, 'metadata', frozen_mapping(metadata))
        object.__setattr__(instance, 'audio', audio)
        return instance

    def as_shadow_record(self) -> ShadowInputRecord:
        """Convert the formal DTO to the only record shape accepted by a shadow sink."""
        metadata = dict(self.metadata)
        if self.audio is not None:
            metadata['audio_ref'] = _media_ref(self.audio)
        return ShadowInputRecord(
            self.input_id, self.correlation_id, 'voice', self.text, self.accepted_at, metadata,
        )


class VoiceShadowAdapter:
    """Non-blocking observer intentionally detached from VAD, ASR, Brain, and TTS."""

    def __init__(
        self,
        sink: ShadowSink,
        *,
        max_queue_size: int = 64,
        sink_timeout: float = 1.0,
        idempotency_store: IdempotencyStore | None = None,
        max_outstanding_sink_calls: int = 4,
        issuer: 'AcceptedInputIssuer',
    ) -> None:
        if not isinstance(issuer, AcceptedInputIssuer):
            raise TypeError('issuer must be an AcceptedInputIssuer')
        self._issuer = issuer
        self._delivery = BoundedShadowAdapter(
            sink,
            max_queue_size=max_queue_size,
            sink_timeout=sink_timeout,
            idempotency_store=idempotency_store,
            max_outstanding_sink_calls=max_outstanding_sink_calls,
        )

    def observe(self, segment: AcceptedVoiceSegment) -> bool:
        """Enqueue only a formally accepted complete segment without waiting."""
        if not isinstance(segment, AcceptedVoiceSegment):
            raise TypeError('segment must be an AcceptedVoiceSegment')
        if not self._issuer.owns(segment):
            return False
        return self._delivery.observe(segment.as_shadow_record())

    def drain(self, *, timeout: float | None = None) -> bool:
        return self._delivery.drain(timeout=timeout)

    def close(self, *, drain: bool = True, timeout: float | None = None) -> bool:
        return self._delivery.close(drain=drain, timeout=timeout)

    def metrics(self) -> ShadowMetrics:
        return self._delivery.metrics()


class AcceptedInputIssuer:
    """Integration-held capability issuer; this is a module boundary, not cryptographic proof."""

    def __init__(self) -> None:
        self._issued: WeakSet[AcceptedVoiceSegment] = WeakSet()

    def issue(
        self,
        input_id: EventId,
        correlation_id: CorrelationId,
        text: str,
        accepted_at: datetime,
        metadata: Mapping[str, FrozenJsonValue] | None = None,
        audio: MediaRef | None = None,
    ) -> AcceptedVoiceSegment:
        """Construct a DTO only after the formal voice integration has accepted the full segment."""
        segment = AcceptedVoiceSegment._create(
            input_id,
            correlation_id,
            text,
            accepted_at,
            metadata,
            audio,
        )
        self._issued.add(segment)
        return segment

    def owns(self, segment: AcceptedVoiceSegment) -> bool:
        return segment in self._issued


def _media_ref(media: MediaRef) -> dict[str, object]:
    return {
        'media_id': media.media_id,
        'sha256': media.sha256,
        'media_type': media.media_type,
        'size_bytes': media.size_bytes,
    }
