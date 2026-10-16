from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.self_loop.contracts import SelfLoopCandidate
from guanjia.self_loop.policy import ExpressionPolicy, ReviewPolicy
from guanjia.self_loop.state import SelfLoopState


def test_review_interval_urgent_and_expression_suppression_are_independent() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    review = ReviewPolicy(review_interval_seconds=1800)
    expression = ExpressionPolicy(daily_budget=1, topic_cooldown_seconds=3600)
    state = SelfLoopState()
    ordinary = SelfLoopCandidate('weather', 'rain soon', 0.8, 0.2, 0.8, 0.1, now)
    assert review.decide(ordinary, state, now).eligible
    state.record_review(now)
    assert not review.decide(ordinary, state, now).eligible
    assert review.decide(ordinary, state, now.replace(hour=1)).eligible
    assert not expression.decide(ordinary, state, now, do_not_disturb=True).eligible
    assert expression.decide(ordinary, state, now).eligible
    state.record_expression(ordinary, now)
    assert not expression.decide(ordinary, state, now).eligible
    urgent = SelfLoopCandidate('safety', 'smoke alert', 0.9, 1.0, 1.0, 0.1, now)
    assert review.decide(urgent, state, now).immediate
    assert not expression.decide(urgent, state, now, gaming=True).eligible


def test_policies_reject_naive_now() -> None:
    candidate = SelfLoopCandidate('weather', 'rain soon', 0.8, 0.2, 0.8, 0.1, datetime(2026, 10, 16, tzinfo=timezone.utc))
    try:
        ReviewPolicy().decide(candidate, SelfLoopState(), datetime(2026, 10, 16))
    except ValueError:
        return
    raise AssertionError('naive now accepted')
