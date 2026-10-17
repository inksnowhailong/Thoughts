"""Strict, versioned wire format for main-agent turn events."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from types import MappingProxyType
from typing import Iterable, Mapping


EVENT_SCHEMA_VERSION = '1.0'
SUPPORTED_EVENT_TYPES = frozenset({
    'reply.delta',
    'reply.silent',
    'task.create',
    'task.control',
    'tool.read',
    'output.request',
    'memory.request',
    'turn.completed',
})


class EventValidationError(ValueError):
    """The complete event batch is unsafe to stage or commit."""


@dataclass(frozen=True)
class MainEvent:
    """A validated event envelope with deeply frozen JSON-only payload."""

    version: str
    event_id: str
    correlation_id: str
    idempotency_key: str
    type: str
    payload: Mapping[str, object]


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f'{name} must be a non-empty string')
    return value


def freeze_json(value: object) -> object:
    """Reject arbitrary objects and return an immutable JSON-value copy."""
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise EventValidationError('JSON floats must be finite')
        return value
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise EventValidationError('JSON object keys must be strings')
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item) for item in value)
    raise EventValidationError('values must be JSON-compatible')


def thaw_json(value: object) -> object:
    """Produce a detached mutable JSON representation for an integration port."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


_PAYLOAD_FIELDS = {
    'reply.delta': {'text'},
    'reply.silent': set(),
    'task.create': {'task_id', 'revision', 'goal'},
    'task.control': {'task_id', 'action'},
    'tool.read': {'resource'},
    'output.request': {'channel', 'text'},
    'memory.request': {'query'},
    'turn.completed': {'turn_id'},
}
_identifier_re = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


def _validate_payload(event_type: str, payload: Mapping[str, object]) -> Mapping[str, object]:
    if set(payload) != _PAYLOAD_FIELDS[event_type]:
        raise EventValidationError(f'{event_type} payload fields do not match schema')
    if event_type == 'reply.delta' and not isinstance(payload['text'], str):
        raise EventValidationError('reply.delta text must be a string')
    if event_type == 'task.create':
        if not isinstance(payload['task_id'], str) or type(payload['revision']) is not int or not isinstance(payload['goal'], str):
            raise EventValidationError('task.create payload is invalid')
    if event_type == 'task.control':
        if not isinstance(payload['task_id'], str) or not isinstance(payload['action'], str):
            raise EventValidationError('task.control payload is invalid')
    if event_type == 'tool.read' and not isinstance(payload['resource'], str):
        raise EventValidationError('tool.read resource must be a string')
    if event_type == 'output.request':
        if not isinstance(payload['channel'], str) or not isinstance(payload['text'], str):
            raise EventValidationError('output.request payload is invalid')
    if event_type == 'memory.request' and not isinstance(payload['query'], str):
        raise EventValidationError('memory.request query must be a string')
    if event_type == 'turn.completed':
        _wire_identifier(payload['turn_id'], 'turn_id')
    frozen = freeze_json(payload)
    assert isinstance(frozen, Mapping)
    return frozen


def _wire_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _identifier_re.fullmatch(value):
        raise EventValidationError(f'{name} has invalid format')
    return value


def parse_event(value: Mapping[str, object]) -> MainEvent:
    """Validate one event without performing any side effect."""
    expected = {'version', 'event_id', 'correlation_id', 'idempotency_key', 'type', 'payload'}
    if set(value) != expected:
        raise EventValidationError('event envelope fields do not match schema')
    version = _identifier(value['version'], 'version')
    if version != EVENT_SCHEMA_VERSION:
        raise EventValidationError(f'unsupported event schema version: {version}')
    event_type = _identifier(value['type'], 'type')
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise EventValidationError(f'unsupported event type: {event_type}')
    payload = value['payload']
    if not isinstance(payload, Mapping):
        raise EventValidationError('payload must be an object')
    return MainEvent(
        version=version,
        event_id=_wire_identifier(value['event_id'], 'event_id'),
        correlation_id=_wire_identifier(value['correlation_id'], 'correlation_id'),
        idempotency_key=_wire_identifier(value['idempotency_key'], 'idempotency_key'),
        type=event_type,
        payload=_validate_payload(event_type, payload),
    )


def parse_events(values: Iterable[Mapping[str, object]]) -> tuple[MainEvent, ...]:
    """Validate a structured event batch."""
    events = tuple(parse_event(value) for value in values)
    if not events:
        raise EventValidationError('event batch must not be empty')
    correlation_ids = {event.correlation_id for event in events}
    if len(correlation_ids) != 1:
        raise EventValidationError('all events must have one correlation id')
    if len({event.event_id for event in events}) != len(events):
        raise EventValidationError('event ids must be unique')
    if len({event.idempotency_key for event in events}) != len(events):
        raise EventValidationError('idempotency keys must be unique')
    if events[-1].type != 'turn.completed' or sum(event.type == 'turn.completed' for event in events) != 1:
        raise EventValidationError('turn.completed must occur exactly once and last')
    kinds = {event.type for event in events[:-1]}
    if 'reply.delta' in kinds and 'reply.silent' in kinds:
        raise EventValidationError('reply.delta and reply.silent are mutually exclusive')
    if 'reply.delta' not in kinds and 'reply.silent' not in kinds:
        raise EventValidationError('turn must include reply.delta or reply.silent')
    return events


def parse_jsonl(source: str | bytes) -> tuple[MainEvent, ...]:
    """Parse JSONL with no blank records and validate every envelope."""
    if isinstance(source, bytes):
        source = source.decode('utf-8')
    if not isinstance(source, str) or not source.strip():
        raise EventValidationError('JSONL source must not be empty')
    records: list[Mapping[str, object]] = []
    for line in source.splitlines():
        if not line.strip():
            raise EventValidationError('blank JSONL records are not allowed')
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise EventValidationError('invalid JSONL record') from error
        if not isinstance(value, Mapping):
            raise EventValidationError('JSONL record must be an object')
        records.append(value)
    return parse_events(records)
