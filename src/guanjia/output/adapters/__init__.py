"""Passive, redacted shadow adapters for comparing legacy output behavior.

These adapters are intentionally not delivery adapters.  They only persist
comparison evidence through an injected sink and never invoke a channel.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from threading import RLock
from typing import Protocol

from guanjia.output.contracts import IdempotencyConflict

_MAX_DEPTH = 8
_MAX_ITEMS = 64
_MAX_STRING = 2_048
_SENSITIVE_KEY = re.compile(
    r'secret|token|password|credential|api_?key|target|command|param|path|file|media|artifact|object_scope',
    re.IGNORECASE,
)
_BEARER = re.compile(r'\bBearer\s+[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE)
_KEY = re.compile(r'\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b', re.IGNORECASE)
_QUERY_SECRET = re.compile(r'([?&](?:token|access_token|api_?key|key|signature|password|secret)=)[^&#\s]*', re.IGNORECASE)
_ASSIGNMENT_SECRET = re.compile(r'\b(password|secret|token|api_?key)\s*([:=])\s*([^\s,;]+)', re.IGNORECASE)
_PATH = re.compile(
    r'(?:file:(?://)?[^\s\'"]+|\\\\[^\s\'"]+|[A-Za-z]:[\\/][^\s\'"]*|(?<!\w)/(?:[^\s\'"]+))',
    re.IGNORECASE,
)
_MEDIA_REF = re.compile(r'\b(?:media|artifact)://[^\s\'"]+|\b[^\s\'"]+\.(?:mp3|mp4|wav|m4a|webm|jpg|jpeg|png|gif|webp)\b', re.IGNORECASE)


class AuditSink(Protocol):
    """Durable idempotency boundary supplied by the host application."""

    def record(self, event_id: str, event: Mapping[str, object]) -> bool:
        """Atomically create or compare redacted semantics, excluding `observed_at`.

        Implementations retain the first `observed_at` value; replay timestamps
        are diagnostic arrival time and never create a second semantic event.
        """


class InMemoryAuditSink:
    """Thread-safe sink useful for tests and for defining the sink contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.events: dict[str, dict[str, object]] = {}

    def record(self, event_id: str, event: Mapping[str, object]) -> bool:
        normalized = _json_value(event)
        with self._lock:
            existing = self.events.get(event_id)
            if existing is None:
                self.events[event_id] = normalized
                return True
            if _event_semantics(existing) != _event_semantics(normalized):
                raise IdempotencyConflict('shadow audit event id belongs to different redacted semantics')
            if existing is not None:
                return False


def to_mapping(value: object) -> dict[str, object]:
    """Convert a narrow DTO/observation to a plain mapping without mutation."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('shadow observation keys must be strings')
        result = dict(value)
        normalized = _json_value(result)
        if not isinstance(normalized, dict):
            raise ValueError('shadow observations must normalize to JSON objects')
        return normalized
    if is_dataclass(value) and not isinstance(value, type):
        result = asdict(value)
        normalized = _json_value(result)
        if not isinstance(normalized, dict):
            raise ValueError('shadow observations must normalize to JSON objects')
        return normalized
    raise ValueError('shadow observations must be JSON mappings or dataclass instances')


def redact(value: object, key: str = '') -> object:
    """Recursively redact JSON-safe evidence, including secrets embedded in prose."""

    return _json_value(value, key)


def _json_value(value: object, key: str = '', depth: int = 0) -> object:
    if depth > _MAX_DEPTH:
        return '<truncated:depth>'
    if _SENSITIVE_KEY.search(key):
        return '<redacted>'
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('shadow observations cannot contain non-finite numbers')
        return value
    if type(value) is str:
        return _redact_text(value)
    if isinstance(value, Mapping):
        if any(not isinstance(name, str) for name in value):
            raise ValueError('shadow observation keys must be strings')
        pairs = list(value.items())
        result = {name: _json_value(item, name, depth + 1) for name, item in pairs[:_MAX_ITEMS]}
        if len(pairs) > _MAX_ITEMS:
            result['__truncated_items__'] = len(pairs) - _MAX_ITEMS
        return result
    if type(value) in (list, tuple):
        result = [_json_value(item, key, depth + 1) for item in value[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            result.append('<truncated:items>')
        return result
    raise ValueError('shadow observations must contain only JSON values')


def _redact_text(value: str) -> str:
    redacted = _BEARER.sub('Bearer <redacted>', value)
    redacted = _KEY.sub('<redacted:key>', redacted)
    redacted = _QUERY_SECRET.sub(r'\1<redacted>', redacted)
    redacted = _ASSIGNMENT_SECRET.sub(r'\1\2<redacted>', redacted)
    redacted = _PATH.sub('<redacted:path>', redacted)
    redacted = _MEDIA_REF.sub('<redacted:media>', redacted)
    return redacted if len(redacted) <= _MAX_STRING else redacted[:_MAX_STRING] + '<truncated:string>'


def _canonical_json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _event_semantics(event: object) -> str:
    """`observed_at` records the first persisted observation and is replay-insensitive."""

    if not isinstance(event, dict):
        raise ValueError('shadow audit events must be JSON objects')
    return _canonical_json({key: value for key, value in event.items() if key != 'observed_at'})


def structured_diff(expected: Mapping[str, object], observed: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """Return deterministic, redacted field-level parity differences."""

    differences: list[dict[str, object]] = []
    for field in sorted(set(expected) | set(observed)):
        left, right = expected.get(field), observed.get(field)
        if left != right:
            differences.append({
                'field': field,
                'expected': redact(left, field),
                'observed': redact(right, field),
            })
    return tuple(differences)


def event_id(adapter: str, kind: str, identity: str) -> str:
    """Produce a stable opaque key; the source identity never enters evidence."""

    digest = sha256(f'{adapter}\0{kind}\0{identity}'.encode('utf-8')).hexdigest()
    return f'shadow:{adapter}:{kind}:{digest}'


def utc_iso(value: object) -> str:
    """Validate and serialize an aware UTC timestamp supplied by a caller."""

    from datetime import datetime, timedelta

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('audit timestamp must be an aware UTC datetime')
    return value.isoformat().replace('+00:00', 'Z')


__all__ = [
    'AuditSink',
    'InMemoryAuditSink',
    'event_id',
    'redact',
    'structured_diff',
    'to_mapping',
    'utc_iso',
]
