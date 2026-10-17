"""Shadow comparison for legacy panel task notices; it registers no routes."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from guanjia.output.contracts import OutputRequest

from . import AuditSink, event_id, redact, structured_diff, to_mapping, utc_iso
from .legacy_voice import ShadowResult


class PanelShadowAdapter:
    """Mirror a task notice into audit evidence without touching the panel."""

    def __init__(self, sink: AuditSink) -> None:
        if not callable(getattr(sink, 'record', None)):
            raise TypeError('sink must implement record(event_id, event)')
        self._sink = sink

    def mirror_task_notice(
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
            'kind': 'task_notice',
            'output_id': request.output_id,
            'task_id': request.authorization.task_id if request.authorization else None,
            'text': request.text,
            'channels': list(request.preferred_channels),
        }
        observed = to_mapping(legacy_observation)
        differences = structured_diff(expected, observed)
        identifier = event_id('panel', 'task_notice', request.output_id)
        event = {
            'schema': 'guanjia.shadow-output.v1',
            'adapter': 'panel',
            'kind': 'task_notice',
            'observed_at': utc_iso(observed_at),
            'expected': redact(expected),
            'observed': redact(observed),
            'differences': list(differences),
        }
        try:
            recorded = self._sink.record(identifier, event)
        except Exception as exc:
            return ShadowResult(identifier, False, differences, type(exc).__name__)
        return ShadowResult(identifier, recorded, differences)


__all__ = ['PanelShadowAdapter']
