"""Passive, bounded adapter for inbound OpenClaw WeChat DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from guanjia.contracts import CorrelationId, EventId, FrozenJsonValue
from guanjia.input.contracts import MediaRef

from ._async_shadow import BoundedShadowAdapter, IdempotencyStore, ShadowMetrics, ShadowSink
from .contracts import ShadowInputRecord, frozen_mapping


@dataclass(frozen=True)
class WeChatInboundMessage:
    """Trusted inbound DTO supplied after OpenClaw has accepted the message."""

    input_id: EventId
    correlation_id: CorrelationId
    sender_id: str
    text: str
    received_at: datetime
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    reply_target: str | None = None
    attachments: tuple[MediaRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.sender_id, str) or not self.sender_id.strip():
            raise ValueError('sender_id must be a non-empty string')
        if self.reply_target is not None and (not isinstance(self.reply_target, str) or not self.reply_target.strip()):
            raise ValueError('reply_target must be a non-empty string or None')
        attachments = tuple(self.attachments)
        if not all(isinstance(item, MediaRef) for item in attachments):
            raise ValueError('attachments must contain MediaRef values')
        record = ShadowInputRecord(
            self.input_id,
            self.correlation_id,
            'wechat',
            self.text,
            self.received_at,
            {**dict(self.metadata), 'sender_id': self.sender_id},
        )
        object.__setattr__(self, 'input_id', record.input_id)
        object.__setattr__(self, 'correlation_id', record.correlation_id)
        object.__setattr__(self, 'text', record.text)
        object.__setattr__(self, 'received_at', record.received_at)
        object.__setattr__(self, 'metadata', frozen_mapping(self.metadata))
        object.__setattr__(self, 'attachments', attachments)

    def as_shadow_record(self) -> ShadowInputRecord:
        """Keep sender provenance as immutable metadata on the write-only shadow record."""
        metadata = {**dict(self.metadata), 'sender_id': self.sender_id}
        if self.reply_target is not None:
            metadata['reply_target'] = self.reply_target
        if self.attachments:
            metadata['attachments'] = tuple(_media_ref(item) for item in self.attachments)
        return ShadowInputRecord(
            self.input_id,
            self.correlation_id,
            'wechat',
            self.text,
            self.received_at,
            metadata,
        )


class WeChatShadowAdapter:
    """Never blocks or controls OpenClaw's live inbound-message path."""

    def __init__(
        self,
        sink: ShadowSink,
        *,
        max_queue_size: int = 64,
        sink_timeout: float = 1.0,
        idempotency_store: IdempotencyStore | None = None,
        max_outstanding_sink_calls: int = 4,
    ) -> None:
        self._delivery = BoundedShadowAdapter(
            sink,
            max_queue_size=max_queue_size,
            sink_timeout=sink_timeout,
            idempotency_store=idempotency_store,
            max_outstanding_sink_calls=max_outstanding_sink_calls,
        )

    def observe(self, message: WeChatInboundMessage) -> bool:
        if not isinstance(message, WeChatInboundMessage):
            raise TypeError('message must be a WeChatInboundMessage')
        return self._delivery.observe(message.as_shadow_record())

    def drain(self, *, timeout: float | None = None) -> bool:
        return self._delivery.drain(timeout=timeout)

    def close(self, *, drain: bool = True, timeout: float | None = None) -> bool:
        return self._delivery.close(drain=drain, timeout=timeout)

    def metrics(self) -> ShadowMetrics:
        return self._delivery.metrics()


def _media_ref(media: MediaRef) -> dict[str, object]:
    return {
        'media_id': media.media_id,
        'sha256': media.sha256,
        'media_type': media.media_type,
        'size_bytes': media.size_bytes,
    }
