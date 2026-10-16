"""Pure DTOs shared by self-loop collection, scoring, and state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class SelfLoopCandidate:
    topic: str
    summary: str
    value: float
    urgency: float
    freshness: float
    interruption_cost: float
    observed_at: datetime
    trigger: str = 'event'
    related_memory_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, name in ((self.topic, 'topic'), (self.summary, 'summary'), (self.trigger, 'trigger')):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{name} must be a non-empty string')
        for score in (self.value, self.urgency, self.freshness, self.interruption_cost):
            if isinstance(score, bool) or type(score) not in (int, float) or not 0.0 <= score <= 1.0:
                raise ValueError('candidate scores must be between 0 and 1')
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timedelta(0):
            raise ValueError('observed_at must be an aware UTC datetime')
        object.__setattr__(self, 'observed_at', self.observed_at.astimezone(timezone.utc))
        ids = tuple(self.related_memory_ids)
        if any(not isinstance(item, str) or not item.strip() for item in ids):
            raise ValueError('related_memory_ids must contain non-empty strings')
        object.__setattr__(self, 'related_memory_ids', ids)

    @property
    def identity(self) -> tuple[str, str]:
        return self.topic.casefold(), self.summary.casefold()


@dataclass(frozen=True)
class PolicyDecision:
    eligible: bool
    immediate: bool
    score: float
    reason: str

    def __post_init__(self) -> None:
        if type(self.eligible) is not bool or type(self.immediate) is not bool:
            raise ValueError('decision flags must be bool')
        if isinstance(self.score, bool) or type(self.score) not in (int, float) or not 0.0 <= self.score <= 1.0:
            raise ValueError('score must be a number between 0 and 1')
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError('reason must be a non-empty string')


@dataclass(frozen=True)
class CompressedSelfLoopState:
    attention_topics: tuple[str, ...]
    unresolved_ideas: tuple[str, ...]
    silenced_topics: tuple[str, ...]
    waiting_conditions: tuple[str, ...]
    recent_expression_topics: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'attention_topics', tuple(self.attention_topics))
        object.__setattr__(self, 'unresolved_ideas', tuple(self.unresolved_ideas))
        object.__setattr__(self, 'silenced_topics', tuple(self.silenced_topics))
        object.__setattr__(self, 'waiting_conditions', tuple(self.waiting_conditions))
        object.__setattr__(self, 'recent_expression_topics', tuple(self.recent_expression_topics))
