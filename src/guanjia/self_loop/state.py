"""Independent, compactable short-term psychological self-loop state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock

from .contracts import CompressedSelfLoopState, SelfLoopCandidate


@dataclass
class SelfLoopState:
    attention: dict[str, tuple[float, datetime]] = field(default_factory=dict)
    unresolved_ideas: dict[str, datetime] = field(default_factory=dict)
    silenced: dict[str, datetime] = field(default_factory=dict)
    waiting_conditions: dict[str, datetime] = field(default_factory=dict)
    expressions: list[tuple[str, datetime]] = field(default_factory=list)
    last_review_at: datetime | None = None
    ttl: timedelta = field(default_factory=lambda: timedelta(days=1))
    expression_limit: int = 100
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ttl, timedelta) or self.ttl <= timedelta(0):
            raise ValueError('ttl must be positive')
        if type(self.expression_limit) is not int or self.expression_limit <= 0:
            raise ValueError('expression_limit must be a positive integer')

    def _validate_at(self, at: datetime) -> datetime:
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError('timestamps must be aware UTC datetimes')
        return at.astimezone(timezone.utc)

    def record_expression(self, candidate: SelfLoopCandidate, at: datetime) -> None:
        at = self._validate_at(at)
        with self._lock:
            self.expressions.append((candidate.topic, at))
            del self.expressions[:-self.expression_limit]
            self.attention[candidate.topic] = (candidate.value, at)

    def record_silence(self, candidate: SelfLoopCandidate, at: datetime) -> None:
        at = self._validate_at(at)
        with self._lock:
            self.silenced[candidate.topic] = at
            self.attention[candidate.topic] = (candidate.value, at)

    def add_unresolved(self, idea: str, at: datetime) -> None:
        with self._lock:
            self.unresolved_ideas[idea] = self._validate_at(at)

    def wait_for(self, condition: str, at: datetime) -> None:
        with self._lock:
            self.waiting_conditions[condition] = self._validate_at(at)

    def record_review(self, at: datetime) -> None:
        with self._lock:
            self.last_review_at = self._validate_at(at)

    def last_expression(self, topic: str) -> datetime | None:
        with self._lock:
            for expressed_topic, at in reversed(self.expressions):
                if expressed_topic == topic:
                    return at
        return None

    def expressions_today(self, now: datetime) -> int:
        now = self._validate_at(now)
        with self._lock:
            return sum(1 for _, at in self.expressions if at.date() == now.date())

    def prune(self, now: datetime) -> None:
        now = self._validate_at(now)
        cutoff = now - self.ttl
        with self._lock:
            self.attention = {key: item for key, item in self.attention.items() if item[1] >= cutoff}
            self.silenced = {key: at for key, at in self.silenced.items() if at >= cutoff}
            self.unresolved_ideas = {key: at for key, at in self.unresolved_ideas.items() if at >= cutoff}
            self.waiting_conditions = {key: at for key, at in self.waiting_conditions.items() if at >= cutoff}
            self.expressions[:] = [(topic, at) for topic, at in self.expressions if at >= cutoff][-self.expression_limit:]

    def compress(self, now: datetime) -> CompressedSelfLoopState:
        """Discard detailed timestamps while retaining compact active-state summaries."""
        self.prune(now)
        with self._lock:
            return CompressedSelfLoopState(
                attention_topics=tuple(sorted(self.attention, key=lambda key: self.attention[key][0], reverse=True)[:32]),
                unresolved_ideas=tuple(self.unresolved_ideas)[:32],
                silenced_topics=tuple(self.silenced)[:32],
                waiting_conditions=tuple(self.waiting_conditions)[:32],
                recent_expression_topics=tuple(topic for topic, _ in self.expressions[-10:]),
            )
