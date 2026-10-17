"""Deterministic eligibility policy; it performs no model or runtime calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .contracts import PolicyDecision, SelfLoopCandidate
from .state import SelfLoopState


class ReviewPolicy:
    def __init__(
        self,
        *,
        minimum_score: float = 0.35,
        urgent_threshold: float = 0.9,
        review_interval_seconds: int = 1800,
    ) -> None:
        _score_config(minimum_score, 'minimum_score')
        _score_config(urgent_threshold, 'urgent_threshold')
        if type(review_interval_seconds) is not int or review_interval_seconds <= 0:
            raise ValueError('review_interval_seconds must be a positive integer')
        self.minimum_score = minimum_score
        self.urgent_threshold = urgent_threshold
        self.review_interval = timedelta(seconds=review_interval_seconds)

    def score(self, candidate: SelfLoopCandidate) -> float:
        return max(0.0, min(1.0, (
            candidate.value * 0.4
            + candidate.urgency * 0.3
            + candidate.freshness * 0.2
            - candidate.interruption_cost * 0.1
        )))

    def decide(self, candidate: SelfLoopCandidate, state: SelfLoopState, now: datetime) -> PolicyDecision:
        """Decide model-review eligibility independent of any user interruption policy."""
        now = _utc_now(now)
        state._reload()
        score = self.score(candidate)
        if candidate.urgency >= self.urgent_threshold:
            return PolicyDecision(True, True, score, 'urgent candidate')
        if state.last_review_at is not None and now - state.last_review_at < self.review_interval:
            return PolicyDecision(False, False, score, 'review interval')
        if score < self.minimum_score:
            return PolicyDecision(False, False, score, 'insufficient value')
        return PolicyDecision(True, False, score, 'eligible for review')


class ExpressionPolicy:
    def __init__(
        self,
        *,
        daily_budget: int = 3,
        topic_cooldown_seconds: int = 3600,
        minimum_score: float = 0.35,
    ) -> None:
        _score_config(minimum_score, 'minimum_score')
        if type(daily_budget) is not int or daily_budget < 0:
            raise ValueError('daily_budget must be a non-negative integer')
        if type(topic_cooldown_seconds) is not int or topic_cooldown_seconds <= 0:
            raise ValueError('topic_cooldown_seconds must be a positive integer')
        self.daily_budget = daily_budget
        self.topic_cooldown = timedelta(seconds=topic_cooldown_seconds)
        self.minimum_score = minimum_score

    def decide(
        self, candidate: SelfLoopCandidate, state: SelfLoopState, now: datetime, *,
        gaming: bool = False, do_not_disturb: bool = False,
    ) -> PolicyDecision:
        now = _utc_now(now)
        state._reload()
        score = max(0.0, min(1.0, (
            candidate.value * 0.4 + candidate.urgency * 0.3
            + candidate.freshness * 0.2 - candidate.interruption_cost * 0.1
        )))
        if gaming or do_not_disturb:
            return PolicyDecision(False, False, score, 'activity suppression')
        last = state.last_expression(candidate.topic)
        if last is not None and now - last < self.topic_cooldown:
            return PolicyDecision(False, False, score, 'topic cooldown')
        if state.expressions_today(now) >= self.daily_budget:
            return PolicyDecision(False, False, score, 'daily budget exhausted')
        if score < self.minimum_score:
            return PolicyDecision(False, False, score, 'insufficient value')
        return PolicyDecision(True, False, score, 'eligible for expression')


class SelfLoopPolicy:
    """Container exposing independent review and expression policies."""

    def __init__(self, *, daily_budget: int = 3, topic_cooldown_seconds: int = 3600,
                 review_window_seconds: int = 1800, minimum_score: float = 0.35,
                 urgent_threshold: float = 0.9) -> None:
        self.review = ReviewPolicy(
            minimum_score=minimum_score, urgent_threshold=urgent_threshold,
            review_interval_seconds=review_window_seconds,
        )
        self.expression = ExpressionPolicy(
            daily_budget=daily_budget, topic_cooldown_seconds=topic_cooldown_seconds,
            minimum_score=minimum_score,
        )

def _score_config(value: float, name: str) -> None:
    if isinstance(value, bool) or type(value) not in (int, float) or not 0.0 <= value <= 1.0:
        raise ValueError(f'{name} must be a number between 0 and 1')


def _utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('now must be an aware UTC datetime')
    return value.astimezone(timezone.utc)
