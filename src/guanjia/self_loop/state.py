"""Independent, compactable short-term psychological self-loop state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

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
    store: Any | None = field(default=None, repr=False, compare=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ttl, timedelta) or self.ttl <= timedelta(0):
            raise ValueError('ttl must be positive')
        if type(self.expression_limit) is not int or self.expression_limit <= 0:
            raise ValueError('expression_limit must be a positive integer')
        self._reload()

    def bind(self, store: Any) -> None:
        """Attach the state facade to the authoritative store and reload it."""
        if not callable(getattr(store, 'load_state', None)) or not callable(getattr(store, 'mutate_state', None)):
            raise ValueError('store must support persistent self-loop state')
        with self._lock:
            self.store = store
            self._reload()

    def _reload(self) -> None:
        if self.store is None:
            return
        stored = self.store.load_state()
        self.attention = dict(stored.attention)
        self.unresolved_ideas = dict(stored.unresolved_ideas)
        self.silenced = dict(stored.silenced)
        self.waiting_conditions = dict(stored.waiting_conditions)
        self.expressions = list(stored.expressions)[-self.expression_limit:]
        self.last_review_at = stored.last_review_at

    def _mutate(self, operation: Any) -> None:
        """Apply an operation to the latest store state in one store transaction."""
        if self.store is None:
            operation(self)
            return
        from .store import StoredState
        def apply(stored: StoredState) -> StoredState:
            working = SelfLoopState(
                attention=dict(stored.attention), unresolved_ideas=dict(stored.unresolved_ideas),
                silenced=dict(stored.silenced), waiting_conditions=dict(stored.waiting_conditions),
                expressions=list(stored.expressions), last_review_at=stored.last_review_at,
                ttl=self.ttl, expression_limit=self.expression_limit,
            )
            operation(working)
            return StoredState(dict(working.attention), dict(working.unresolved_ideas), dict(working.silenced),
                               dict(working.waiting_conditions), tuple(working.expressions), working.last_review_at)
        stored = self.store.mutate_state(apply)
        self.attention, self.unresolved_ideas = dict(stored.attention), dict(stored.unresolved_ideas)
        self.silenced, self.waiting_conditions = dict(stored.silenced), dict(stored.waiting_conditions)
        self.expressions, self.last_review_at = list(stored.expressions), stored.last_review_at

    def _validate_at(self, at: datetime) -> datetime:
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError('timestamps must be aware UTC datetimes')
        return at.astimezone(timezone.utc)

    def record_expression(self, candidate: SelfLoopCandidate, at: datetime) -> None:
        at = self._validate_at(at)
        with self._lock:
            self._mutate(lambda state: (state.expressions.append((candidate.topic, at)),
                                        state.expressions.__delitem__(slice(None, -state.expression_limit)),
                                        state.attention.__setitem__(candidate.topic, (candidate.value, at))))

    def record_silence(self, candidate: SelfLoopCandidate, at: datetime) -> None:
        at = self._validate_at(at)
        with self._lock:
            self._mutate(lambda state: (state.silenced.__setitem__(candidate.topic, at),
                                        state.attention.__setitem__(candidate.topic, (candidate.value, at))))

    def add_unresolved(self, idea: str, at: datetime) -> None:
        with self._lock:
            at = self._validate_at(at)
            self._mutate(lambda state: state.unresolved_ideas.__setitem__(idea, at))

    def wait_for(self, condition: str, at: datetime) -> None:
        with self._lock:
            at = self._validate_at(at)
            self._mutate(lambda state: state.waiting_conditions.__setitem__(condition, at))

    def record_review(self, at: datetime) -> None:
        with self._lock:
            at = self._validate_at(at)
            self._mutate(lambda state: setattr(state, 'last_review_at', at))

    def last_expression(self, topic: str) -> datetime | None:
        with self._lock:
            self._reload()
            for expressed_topic, at in reversed(self.expressions):
                if expressed_topic == topic:
                    return at
        return None

    def expressions_today(self, now: datetime) -> int:
        now = self._validate_at(now)
        with self._lock:
            self._reload()
            return sum(1 for _, at in self.expressions if at.date() == now.date())

    def prune(self, now: datetime) -> None:
        now = self._validate_at(now)
        cutoff = now - self.ttl
        with self._lock:
            def prune(state: SelfLoopState) -> None:
                state.attention = {key: item for key, item in state.attention.items() if item[1] >= cutoff}
                state.silenced = {key: value for key, value in state.silenced.items() if value >= cutoff}
                state.unresolved_ideas = {key: value for key, value in state.unresolved_ideas.items() if value >= cutoff}
                state.waiting_conditions = {key: value for key, value in state.waiting_conditions.items() if value >= cutoff}
                state.expressions[:] = [(topic, value) for topic, value in state.expressions if value >= cutoff][-state.expression_limit:]
            self._mutate(prune)

    def compress(self, now: datetime) -> CompressedSelfLoopState:
        """Discard detailed timestamps while retaining compact active-state summaries."""
        self.prune(now)
        with self._lock:
            self._reload()
            return CompressedSelfLoopState(
                attention_topics=tuple(sorted(self.attention, key=lambda key: self.attention[key][0], reverse=True)[:32]),
                unresolved_ideas=tuple(self.unresolved_ideas)[:32],
                silenced_topics=tuple(self.silenced)[:32],
                waiting_conditions=tuple(self.waiting_conditions)[:32],
                recent_expression_topics=tuple(topic for topic, _ in self.expressions[-10:]),
            )
