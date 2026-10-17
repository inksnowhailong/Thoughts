"""Shadow comparison for WeChat permission prompts; it never sends WeChat."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from guanjia.permission.contracts import AuthorizationRequest, PermissionDecision

from . import AuditSink, event_id, redact, structured_diff, to_mapping, utc_iso
from .legacy_voice import ShadowResult


class WechatShadowAdapter:
    """Mirror a legacy permission request into audit evidence only."""

    def __init__(self, sink: AuditSink) -> None:
        if not callable(getattr(sink, 'record', None)):
            raise TypeError('sink must implement record(event_id, event)')
        self._sink = sink

    def mirror_permission_request(
        self,
        request: AuthorizationRequest,
        decision: PermissionDecision,
        legacy_observation: Mapping[str, object],
        observed_at: datetime,
    ) -> ShadowResult:
        if not isinstance(request, AuthorizationRequest) or not isinstance(decision, PermissionDecision):
            raise TypeError('request and decision must use permission domain contracts')
        expected = {
            'kind': 'permission_request',
            'request_id': request.request_id,
            'actor_id': request.actor.actor_id,
            'session_id': request.session_id,
            'task_id': request.task_id,
            'outcome': decision.outcome.value,
            'todo_id': decision.todo_id,
        }
        observed = to_mapping(legacy_observation)
        differences = structured_diff(expected, observed)
        identifier = event_id('wechat', 'permission_request', request.request_id)
        event = {
            'schema': 'guanjia.shadow-output.v1',
            'adapter': 'wechat',
            'kind': 'permission_request',
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


__all__ = ['WechatShadowAdapter']
