"""Bounded sleep-time snapshots of self-loop state, with no persistence side effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .state import SelfLoopState


@dataclass(frozen=True)
class InternalThoughtSummary:
    """Compact hand-off for a caller that owns mainline or memory persistence."""

    attention_topics: tuple[str, ...]
    unresolved_ideas: tuple[str, ...]
    silenced_candidates: tuple[str, ...]
    expression_records: tuple[str, ...]
    waiting_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            'attention_topics',
            'unresolved_ideas',
            'silenced_candidates',
            'expression_records',
            'waiting_conditions',
        ):
            values = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f'{name} must contain non-empty strings')
            object.__setattr__(self, name, values)


def compress_for_sleep(
    state: SelfLoopState,
    now: datetime,
    *,
    max_items: int = 32,
    max_expression_records: int = 10,
) -> InternalThoughtSummary:
    """Create a bounded immutable snapshot; callers decide whether and where to persist it."""
    if not isinstance(state, SelfLoopState):
        raise ValueError('state must be a SelfLoopState')
    now = _utc(now)
    if type(max_items) is not int or max_items <= 0:
        raise ValueError('max_items must be a positive integer')
    if type(max_expression_records) is not int or max_expression_records <= 0:
        raise ValueError('max_expression_records must be a positive integer')
    compact = state.compress(now)
    return InternalThoughtSummary(
        attention_topics=compact.attention_topics[:max_items],
        unresolved_ideas=compact.unresolved_ideas[:max_items],
        silenced_candidates=compact.silenced_topics[:max_items],
        expression_records=compact.recent_expression_topics[-max_expression_records:],
        waiting_conditions=compact.waiting_conditions[:max_items],
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('now must be an aware UTC datetime')
    return value.astimezone(timezone.utc)
