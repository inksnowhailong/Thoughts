"""Shadow comparison for legacy voice replies; this module never calls TTS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from guanjia.output.contracts import OutputRequest

from . import AuditSink, event_id, redact, structured_diff, to_mapping, utc_iso


@dataclass(frozen=True)
class ShadowResult:
    """Outcome of a best-effort audit write, never a delivery outcome."""

    event_id: str
    recorded: bool
    differences: tuple[dict[str, object], ...]
    isolated_error: str | None = None


class LegacyVoiceShadowAdapter:
    """Mirror an observed reply into audit evidence without speaking it."""

    def __init__(self, sink: AuditSink) -> None:
        if not callable(getattr(sink, 'record', None)):
            raise TypeError('sink must implement record(event_id, event)')
        self._sink = sink

    def mirror_reply(
        self,
        request: OutputRequest,
        legacy_observation: Mapping[str, object],
        observed_at: datetime,
    ) -> ShadowResult:
        if not isinstance(request, OutputRequest):
            raise TypeError('request must be an OutputRequest')
        if request.authorization is None:
            raise ValueError('shadow output requires Wave 1 authorization context')
        expected = {
            'kind': 'reply',
            'output_id': request.output_id,
            'actor_id': request.actor_id,
            'conversation_id': request.conversation_id,
            'text': request.text,
            'channels': list(request.preferred_channels),
        }
        observed = to_mapping(legacy_observation)
        differences = structured_diff(expected, observed)
        identifier = event_id('legacy_voice', 'reply', request.output_id)
        event = {
            'schema': 'guanjia.shadow-output.v1',
            'adapter': 'legacy_voice',
            'kind': 'reply',
            'observed_at': utc_iso(observed_at),
            'expected': redact(expected),
            'observed': redact(observed),
            'differences': list(differences),
        }
        try:
            recorded = self._sink.record(identifier, event)
        except Exception as exc:  # Audit is observational and must not break legacy flow.
            return ShadowResult(identifier, False, differences, type(exc).__name__)
        return ShadowResult(identifier, recorded, differences)


__all__ = ['LegacyVoiceShadowAdapter', 'ShadowResult']
