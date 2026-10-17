"""Pure, backend-independent contracts for the unified memory store."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4


class MemorySource(StrEnum):
    USER = 'user'
    USER_CORRECTION = 'user_correction'
    OBSERVATION = 'observation'
    TASK = 'task'
    SELF_LOOP = 'self_loop'
    SYSTEM = 'system'


class RetentionLevel(StrEnum):
    PERMANENT = 'permanent'
    LONG_TERM = 'long_term'
    ADAPTIVE = 'adaptive'
    TEMPORARY = 'temporary'


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{name} must be an aware UTC datetime')
    return value.astimezone(timezone.utc)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value


def _score(value: float, name: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float) or not 0.0 <= value <= 1.0:
        raise ValueError(f'{name} must be a number between 0 and 1')
    return float(value)


def _freeze_metadata(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError('metadata must be a mapping')
    if any(not isinstance(key, str) or not key.strip() or not isinstance(item, str) for key, item in value.items()):
        raise ValueError('metadata keys must be non-empty strings and values must be strings')
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class EvidenceRef:
    reference: str
    source: MemorySource
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.reference, 'reference')
        if not isinstance(self.source, MemorySource):
            raise ValueError('source must be a MemorySource')
        if self.observed_at is not None:
            object.__setattr__(self, 'observed_at', _utc(self.observed_at, 'observed_at'))


@dataclass(frozen=True)
class MediaRef:
    media_id: str
    media_type: str
    content_hash: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.media_id, 'media_id')
        _text(self.media_type, 'media_type')
        _text(self.content_hash, 'content_hash')
        object.__setattr__(self, 'metadata', _freeze_metadata(self.metadata))


@dataclass(frozen=True)
class MemoryCandidate:
    key: str
    content: str
    memory_type: str
    source: MemorySource
    confidence: float
    importance: int
    retention: RetentionLevel = RetentionLevel.ADAPTIVE
    people: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    media: tuple[MediaRef, ...] = ()
    conflict_key: str | None = None

    def __post_init__(self) -> None:
        _text(self.key, 'key')
        _text(self.content, 'content')
        _text(self.memory_type, 'memory_type')
        if not isinstance(self.source, MemorySource):
            raise ValueError('source must be a MemorySource')
        if not isinstance(self.retention, RetentionLevel):
            raise ValueError('retention must be a RetentionLevel')
        object.__setattr__(self, 'confidence', _score(self.confidence, 'confidence'))
        if type(self.importance) is not int or not 0 <= self.importance <= 10:
            raise ValueError('importance must be between 0 and 10')
        people = tuple(dict.fromkeys(self.people))
        if any(not isinstance(person, str) or not person.strip() for person in people):
            raise ValueError('people must contain non-empty strings')
        object.__setattr__(self, 'people', people)
        evidence = tuple(self.evidence)
        media = tuple(self.media)
        if not all(isinstance(item, EvidenceRef) for item in evidence):
            raise ValueError('evidence must contain EvidenceRef values')
        if not all(isinstance(item, MediaRef) for item in media):
            raise ValueError('media must contain MediaRef values')
        if self.conflict_key is not None:
            _text(self.conflict_key, 'conflict_key')
        object.__setattr__(self, 'evidence', evidence)
        object.__setattr__(self, 'media', media)


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    key: str
    content: str
    memory_type: str
    source: MemorySource
    confidence: float
    importance: int
    retention: RetentionLevel
    people: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    media: tuple[MediaRef, ...] = ()
    conflict_key: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime | None = None
    use_count: int = 0

    def __post_init__(self) -> None:
        _text(self.id, 'id')
        _text(self.key, 'key')
        _text(self.content, 'content')
        _text(self.memory_type, 'memory_type')
        if not isinstance(self.source, MemorySource):
            raise ValueError('source must be a MemorySource')
        if not isinstance(self.retention, RetentionLevel):
            raise ValueError('retention must be a RetentionLevel')
        object.__setattr__(self, 'confidence', _score(self.confidence, 'confidence'))
        if type(self.importance) is not int or not 0 <= self.importance <= 10:
            raise ValueError('importance must be between 0 and 10')
        if type(self.use_count) is not int or self.use_count < 0:
            raise ValueError('use_count must be a non-negative integer')
        if self.conflict_key is not None:
            _text(self.conflict_key, 'conflict_key')
        people = tuple(self.people)
        evidence = tuple(self.evidence)
        media = tuple(self.media)
        if any(not isinstance(item, str) or not item.strip() for item in people):
            raise ValueError('people must contain non-empty strings')
        if not all(isinstance(item, EvidenceRef) for item in evidence):
            raise ValueError('evidence must contain EvidenceRef values')
        if not all(isinstance(item, MediaRef) for item in media):
            raise ValueError('media must contain MediaRef values')
        object.__setattr__(self, 'people', people)
        object.__setattr__(self, 'evidence', evidence)
        object.__setattr__(self, 'media', media)
        object.__setattr__(self, 'created_at', _utc(self.created_at, 'created_at'))
        object.__setattr__(self, 'updated_at', _utc(self.updated_at, 'updated_at'))
        if self.last_used_at is not None:
            object.__setattr__(self, 'last_used_at', _utc(self.last_used_at, 'last_used_at'))
        if self.created_at > self.updated_at:
            raise ValueError('created_at cannot be after updated_at')
        if self.last_used_at is not None and self.last_used_at < self.created_at:
            raise ValueError('last_used_at cannot be before created_at')

    @classmethod
    def from_candidate(cls, candidate: MemoryCandidate, record_id: str | None = None) -> 'MemoryRecord':
        return cls(
            id=record_id or str(uuid4()),
            key=candidate.key,
            content=candidate.content,
            memory_type=candidate.memory_type,
            source=candidate.source,
            confidence=candidate.confidence,
            importance=candidate.importance,
            retention=candidate.retention,
            people=candidate.people,
            evidence=candidate.evidence,
            media=candidate.media,
            conflict_key=candidate.conflict_key,
        )

    def replace_with(self, candidate: MemoryCandidate) -> 'MemoryRecord':
        return replace(
            self,
            content=candidate.content,
            memory_type=candidate.memory_type,
            source=candidate.source,
            confidence=candidate.confidence,
            importance=candidate.importance,
            retention=candidate.retention,
            people=candidate.people,
            evidence=candidate.evidence,
            media=candidate.media,
            conflict_key=candidate.conflict_key,
            updated_at=datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class MemoryQuery:
    text: str = ''
    people: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    limit: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError('text must be a string')
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError('limit must be an integer between 1 and 100')
        people = tuple(self.people)
        types = tuple(self.types)
        if any(not isinstance(value, str) or not value.strip() for value in people + types):
            raise ValueError('query people and types must contain non-empty strings')
        object.__setattr__(self, 'people', people)
        object.__setattr__(self, 'types', types)


@dataclass(frozen=True)
class MemoryPacket:
    records: tuple[MemoryRecord, ...]
    query: MemoryQuery = field(default_factory=MemoryQuery)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'records', tuple(self.records))


@dataclass(frozen=True)
class ConflictCase:
    id: str
    key: str
    current: MemoryRecord
    incoming: MemoryCandidate
    status: str = 'uncertain'
    reason: str = 'confidence values are too close'
    current_hash: str = ''

    def __post_init__(self) -> None:
        _text(self.id, 'id')
        _text(self.key, 'key')
        if self.status != 'uncertain':
            raise ValueError('invalid conflict status')
        if self.current.key != self.key or self.incoming.key != self.key:
            raise ValueError('conflict keys must match')
        if not isinstance(self.current_hash, str):
            raise ValueError('current_hash must be a string')


@dataclass(frozen=True)
class MaintenancePlan:
    record_ids: tuple[str, ...]
    action: str
    reason: str

    def __post_init__(self) -> None:
        _text(self.action, 'action')
        _text(self.reason, 'reason')
        object.__setattr__(self, 'record_ids', tuple(self.record_ids))


@dataclass(frozen=True)
class CapabilityProposal:
    title: str
    rationale: str
    evidence: tuple[EvidenceRef, ...]
    status: str = 'pending_approval'

    def __post_init__(self) -> None:
        _text(self.title, 'title')
        _text(self.rationale, 'rationale')
        evidence = tuple(self.evidence)
        if not all(isinstance(item, EvidenceRef) for item in evidence):
            raise ValueError('evidence must contain EvidenceRef values')
        if self.status not in {'pending_approval', 'approved', 'rejected'}:
            raise ValueError('invalid capability proposal status')
        object.__setattr__(self, 'evidence', evidence)
