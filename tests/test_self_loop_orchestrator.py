from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.self_loop.collector import CandidateCollector
from guanjia.self_loop.compress import compress_for_sleep
from guanjia.self_loop.contracts import SelfLoopCandidate
from guanjia.self_loop.orchestrator import ExpressionCandidate, ReviewedThought, SelfLoopOrchestrator
from guanjia.self_loop.policy import ExpressionPolicy, ReviewPolicy
from guanjia.self_loop.state import SelfLoopState
from guanjia.self_loop.store import InMemoryStateStore, SQLiteStateStore


class RecordingReviewer:
    def __init__(self, result: tuple[ReviewedThought, ...] = (), error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def review(self, candidates: tuple[SelfLoopCandidate, ...], *, now: datetime, context: object) -> tuple[ReviewedThought, ...]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _candidate(now: datetime, *, topic: str = 'weather', urgency: float = 0.2) -> SelfLoopCandidate:
    return SelfLoopCandidate(topic, 'rain soon', 0.8, urgency, 0.8, 0.1, now)


def _orchestrator(reviewer: RecordingReviewer) -> tuple[SelfLoopOrchestrator, CandidateCollector]:
    collector = CandidateCollector()
    return (
        SelfLoopOrchestrator(
            collector,
            ReviewPolicy(review_interval_seconds=1800),
            ExpressionPolicy(daily_budget=1),
            SelfLoopState(),
            reviewer,
        ),
        collector,
    )


def test_dnd_allows_review_but_suppresses_expression() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    reviewer = RecordingReviewer((ReviewedThought(source, 'remember rain', expression='rain is coming'),))
    orchestrator, _ = _orchestrator(reviewer)
    orchestrator.collect(source)

    result = orchestrator.evaluate(now, {'do_not_disturb': True})

    assert reviewer.calls == 1
    assert result.internal_inputs[0].role == 'internal_thought'
    assert result.internal_inputs[0].source == 'self_loop'
    assert not result.expression_candidates


def test_review_failure_retains_collector_candidate_and_records_failure() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    reviewer = RecordingReviewer(error=RuntimeError('review unavailable'))
    orchestrator, collector = _orchestrator(reviewer)
    candidate = _candidate(now)
    orchestrator.collect(candidate)

    result = orchestrator.evaluate(now, {})

    assert len(collector) == 1
    assert result.failures[0].error_type == 'RuntimeError'
    assert result.failures[0].message == 'review failed; details redacted'


def test_successful_review_explicitly_acknowledges_candidates_and_emits_task() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    reviewer = RecordingReviewer((ReviewedThought(source, 'check umbrella', proactive_task='prepare umbrella reminder'),))
    orchestrator, collector = _orchestrator(reviewer)
    orchestrator.collect(source)

    result = orchestrator.evaluate(now, {})

    assert not len(collector)
    assert result.proactive_task_candidates[0].source == 'self_loop'


def test_urgent_candidate_bypasses_review_interval() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    urgent = _candidate(now, urgency=1.0)
    reviewer = RecordingReviewer((ReviewedThought(urgent),))
    orchestrator, _ = _orchestrator(reviewer)
    orchestrator.state.record_review(now)
    orchestrator.collect(urgent)

    orchestrator.evaluate(now, {})

    assert reviewer.calls == 1


def test_partial_or_duplicate_reviewer_results_fail_without_consuming_candidates() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    first = _candidate(now, topic='weather')
    second = _candidate(now, topic='calendar')
    reviewer = RecordingReviewer((ReviewedThought(first),))
    orchestrator, collector = _orchestrator(reviewer)
    orchestrator.collect(first)
    orchestrator.collect(second)

    partial = orchestrator.evaluate(now, {})
    assert len(collector) == 2
    assert partial.failures[0].error_type == 'ValueError'

    reviewer.result = (ReviewedThought(first), ReviewedThought(first))
    duplicate = orchestrator.evaluate(now, {})
    assert len(collector) == 2
    assert duplicate.failures[0].error_type == 'ValueError'


def test_silent_result_acks_but_retained_result_does_not() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    silent = _candidate(now)
    reviewer = RecordingReviewer((ReviewedThought(silent),))
    orchestrator, collector = _orchestrator(reviewer)
    orchestrator.collect(silent)

    assert not orchestrator.evaluate(now, {}).internal_inputs
    assert not len(collector)

    retained = _candidate(now, topic='calendar')
    reviewer.result = (ReviewedThought(retained, retain_candidate=True),)
    orchestrator.collect(retained)
    orchestrator.evaluate(now.replace(hour=1), {})
    assert len(collector) == 1


def test_expression_budget_is_recorded_only_after_delivery_ack() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    reviewer = RecordingReviewer((ReviewedThought(source, expression='rain is coming'),))
    orchestrator, _ = _orchestrator(reviewer)
    orchestrator.collect(source)

    result = orchestrator.evaluate(now, {})
    assert len(result.expression_candidates) == 1
    assert orchestrator.state.expressions_today(now) == 0
    assert orchestrator.expression_policy.decide(source, orchestrator.state, now).eligible

    orchestrator.ack_expression_delivered(result.expression_candidates[0], now)
    assert orchestrator.state.expressions_today(now) == 1
    assert not orchestrator.expression_policy.decide(source, orchestrator.state, now).eligible


def test_expression_delivery_ack_is_idempotent_and_bounded() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    reviewer = RecordingReviewer((ReviewedThought(source, expression='rain is coming'),))
    orchestrator, _ = _orchestrator(reviewer)
    orchestrator.collect(source)
    expression = orchestrator.evaluate(now, {}).expression_candidates[0]

    outcomes: list[bool] = []
    threads = [Thread(target=lambda: outcomes.append(orchestrator.ack_expression_delivered(expression, now))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [False, True]
    assert orchestrator.state.expressions_today(now) == 1
    assert expression.expression_id == ExpressionCandidate(expression.candidate, 'rain is coming').expression_id

    second = ExpressionCandidate(source, 'carry an umbrella')
    assert not orchestrator.ack_expression_delivered(second, now)
    assert orchestrator.state.expressions_today(now) == 1


def test_concurrent_evaluations_review_a_candidate_once() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    entered = Event()
    release = Event()

    class BlockingReviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, candidates: tuple[SelfLoopCandidate, ...], *, now: datetime, context: object) -> tuple[ReviewedThought, ...]:
            self.calls += 1
            entered.set()
            assert release.wait(1)
            return (ReviewedThought(source),)

    reviewer = BlockingReviewer()
    orchestrator, _ = _orchestrator(reviewer)
    orchestrator.collect(source)

    first = Thread(target=lambda: orchestrator.evaluate(now, {}))
    first.start()
    assert entered.wait(1)
    busy = orchestrator.evaluate(now, {})
    release.set()
    first.join()

    assert reviewer.calls == 1
    assert busy.busy


def test_collect_during_review_preserves_newer_same_identity_candidate() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    original = _candidate(now)
    entered = Event()
    release = Event()

    class BlockingReviewer:
        def review(self, candidates: tuple[SelfLoopCandidate, ...], *, now: datetime, context: object) -> tuple[ReviewedThought, ...]:
            entered.set()
            assert release.wait(1)
            return (ReviewedThought(original),)

    orchestrator, collector = _orchestrator(BlockingReviewer())
    orchestrator.collect(original)
    results = []
    thread = Thread(target=lambda: results.append(orchestrator.evaluate(now, {})))
    thread.start()
    assert entered.wait(1)
    replacement = SelfLoopCandidate('weather', 'rain soon', 0.9, 0.2, 0.8, 0.1, now)
    orchestrator.collect(replacement)
    release.set()
    thread.join()

    assert collector.snapshot()[0].identity == replacement.identity
    assert results[0].stale_count == 1
    assert not results[0].internal_inputs
    assert not results[0].proactive_task_candidates
    assert not results[0].expression_candidates


def test_expression_policy_failure_preserves_every_reviewed_candidate() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)

    class FailingExpressionPolicy(ExpressionPolicy):
        def decide(
            self,
            candidate: SelfLoopCandidate,
            state: SelfLoopState,
            now: datetime,
            *,
            gaming: bool = False,
            do_not_disturb: bool = False,
        ) -> object:
            raise RuntimeError('policy failure')

    reviewer = RecordingReviewer((ReviewedThought(source, expression='rain is coming'),))
    collector = CandidateCollector()
    orchestrator = SelfLoopOrchestrator(
        collector,
        ReviewPolicy(),
        FailingExpressionPolicy(),
        SelfLoopState(),
        reviewer,
    )
    orchestrator.collect(source)

    result = orchestrator.evaluate(now, {})

    assert result.failures[0].error_type == 'RuntimeError'
    assert collector.snapshot()[0].identity == source.identity


def test_same_field_candidate_readd_has_new_generation_identity() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    collector = CandidateCollector()
    first = _candidate(now)
    collector.add(first)
    current = collector.snapshot()[0]
    assert collector.ack(current)
    replacement = _candidate(now)
    collector.add(replacement)

    assert replacement.candidate_id == ''
    assert replacement.generation == 0
    assert replacement.sequence == 0
    assert collector.snapshot()[0].generation > current.generation


def test_sqlite_restart_persists_ack_and_delivery_budget() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    with TemporaryDirectory() as directory:
        path = str(Path(directory) / 'self-loop.db')
        store = SQLiteStateStore(path)
        collector = CandidateCollector(store)
        reviewer = RecordingReviewer((ReviewedThought(source, expression='rain is coming'),))
        first = SelfLoopOrchestrator(collector, ReviewPolicy(), ExpressionPolicy(daily_budget=1), SelfLoopState(), reviewer)
        first.collect(source)
        expression = first.evaluate(now, {}).expression_candidates[0]

        restarted_store = SQLiteStateStore(path)
        restarted = SelfLoopOrchestrator(
            CandidateCollector(restarted_store),
            ReviewPolicy(),
            ExpressionPolicy(daily_budget=1),
            SelfLoopState(),
            reviewer,
        )
        assert restarted.ack_expression_delivered(expression, now)
        assert not restarted.ack_expression_delivered(expression, now)
        assert not restarted.collector.snapshot()
        store.close()
        restarted_store.close()


def test_shared_store_lease_allows_only_one_orchestrator_review() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    store = InMemoryStateStore()
    entered, release = Event(), Event()

    class BlockingReviewer:
        def review(self, candidates: tuple[SelfLoopCandidate, ...], *, now: datetime, context: object) -> tuple[ReviewedThought, ...]:
            entered.set()
            assert release.wait(1)
            return (ReviewedThought(source),)

    first = SelfLoopOrchestrator(CandidateCollector(store), ReviewPolicy(), ExpressionPolicy(), SelfLoopState(), BlockingReviewer())
    second = SelfLoopOrchestrator(CandidateCollector(store), ReviewPolicy(), ExpressionPolicy(), SelfLoopState(), RecordingReviewer((ReviewedThought(source),)))
    first.collect(source)
    thread = Thread(target=lambda: first.evaluate(now, {}))
    thread.start()
    assert entered.wait(1)
    assert second.evaluate(now, {}).busy
    release.set()
    thread.join()


def test_daily_budget_reserves_at_most_one_expression_per_batch() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    first = _candidate(now, topic='weather')
    second = _candidate(now, topic='calendar')
    reviewer = RecordingReviewer((
        ReviewedThought(first, expression='rain is coming'),
        ReviewedThought(second, expression='meeting soon'),
    ))
    collector = CandidateCollector(InMemoryStateStore())
    orchestrator = SelfLoopOrchestrator(
        collector,
        ReviewPolicy(),
        ExpressionPolicy(daily_budget=1),
        SelfLoopState(),
        reviewer,
    )
    orchestrator.collect(first)
    orchestrator.collect(second)

    result = orchestrator.evaluate(now, {})

    assert len(result.expression_candidates) == 1
    assert result.expression_candidates[0].reservation_id


def test_expired_review_lease_recovers_after_owner_crash() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    store = InMemoryStateStore()
    assert store.acquire_review_lease('crashed-owner', now, now.replace(second=5))
    assert not store.acquire_review_lease('new-owner', now.replace(second=1), now.replace(second=10))
    assert store.acquire_review_lease('new-owner', now.replace(second=6), now.replace(second=20))


def test_completion_clock_rejects_model_result_after_lease_expiry() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)

    class Reviewer:
        def review(self, candidates: tuple[SelfLoopCandidate, ...], *, now: datetime, context: object) -> tuple[ReviewedThought, ...]:
            return (ReviewedThought(candidates[0]),)

    collector = CandidateCollector()
    orchestrator = SelfLoopOrchestrator(
        collector, ReviewPolicy(), ExpressionPolicy(), SelfLoopState(), Reviewer(),
        review_lease_seconds=1, utc_clock=lambda: now.replace(second=2),
    )
    orchestrator.collect(source)
    result = orchestrator.evaluate(now, {})
    assert result.failures[0].error_type == 'RuntimeError'
    assert len(collector) == 1


def test_non_boolean_or_non_mapping_activity_context_is_rejected() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    source = _candidate(now)
    reviewer = RecordingReviewer((ReviewedThought(source),))
    orchestrator, collector = _orchestrator(reviewer)
    orchestrator.collect(source)

    for context in ('false', {'do_not_disturb': 'false'}):
        try:
            orchestrator.evaluate(now, context)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError('invalid activity context accepted')
    assert len(collector) == 1


def test_sleep_compression_is_bounded_and_does_not_persist() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    state = SelfLoopState()
    for index in range(4):
        candidate = SelfLoopCandidate(f'topic-{index}', 'update', 0.5, 0.1, 0.5, 0.1, now)
        state.record_silence(candidate, now)
        state.record_expression(candidate, now)
        state.add_unresolved(f'idea-{index}', now)
        state.wait_for(f'condition-{index}', now)

    summary = compress_for_sleep(state, now, max_items=2, max_expression_records=1)

    assert len(summary.attention_topics) == 2
    assert len(summary.silenced_candidates) == 2
    assert len(summary.unresolved_ideas) == 2
    assert len(summary.waiting_conditions) == 2
    assert len(summary.expression_records) == 1
