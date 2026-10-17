"""Model-free orchestration boundary for self-loop review candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from threading import RLock
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from .collector import CandidateCollector
from .contracts import SelfLoopCandidate
from .policy import ExpressionPolicy, ReviewPolicy
from .state import SelfLoopState
from .store import OutboxItem, ReviewLease, StateStore


@dataclass(frozen=True)
class ReviewedThought:
    """A reviewer proposal; it cannot itself send output or execute a task."""

    candidate: SelfLoopCandidate
    internal_thought: str | None = None
    proactive_task: str | None = None
    expression: str | None = None
    retain_candidate: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SelfLoopCandidate):
            raise ValueError('candidate must be a SelfLoopCandidate')
        for name in ('internal_thought', 'proactive_task', 'expression'):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f'{name} must be a non-empty string when provided')
        if type(self.retain_candidate) is not bool:
            raise ValueError('retain_candidate must be bool')


class ThoughtReviewer(Protocol):
    """Injected model adapter; implementations own all model/network interaction."""

    def review(
        self,
        candidates: tuple[SelfLoopCandidate, ...],
        *,
        now: datetime,
        context: object,
    ) -> tuple[ReviewedThought, ...]: ...


@dataclass(frozen=True)
class InternalInputCandidate:
    candidate: SelfLoopCandidate
    content: str
    role: str = 'internal_thought'
    source: str = 'self_loop'


@dataclass(frozen=True)
class ProactiveTaskCandidate:
    candidate: SelfLoopCandidate
    task: str
    source: str = 'self_loop'


@dataclass(frozen=True)
class ExpressionCandidate:
    candidate: SelfLoopCandidate
    content: str
    source: str = 'self_loop'
    expression_id: str = ''
    reservation_id: str = ''

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SelfLoopCandidate):
            raise ValueError('candidate must be a SelfLoopCandidate')
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError('content must be a non-empty string')
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError('source must be a non-empty string')
        if not isinstance(self.expression_id, str):
            raise ValueError('expression_id must be a string')
        if not isinstance(self.reservation_id, str):
            raise ValueError('reservation_id must be a string')
        if self.expression_id:
            if not self.expression_id.strip():
                raise ValueError('expression_id must be a non-empty string')
        else:
            identity = '\x1f'.join((
                self.candidate.candidate_id,
                self.candidate.observed_at.isoformat(),
                self.content,
            ))
            object.__setattr__(self, 'expression_id', f'self-loop-{sha256(identity.encode()).hexdigest()}')
        if not self.reservation_id:
            object.__setattr__(self, 'reservation_id', f'unreserved-{self.expression_id}')
        elif not self.reservation_id.strip():
            raise ValueError('reservation_id must be a non-empty string')


@dataclass(frozen=True)
class ReviewFailure:
    candidates: tuple[SelfLoopCandidate, ...]
    error_type: str
    message: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.candidates or not all(isinstance(candidate, SelfLoopCandidate) for candidate in self.candidates):
            raise ValueError('candidates must contain SelfLoopCandidate values')
        if not isinstance(self.error_type, str) or not self.error_type.strip():
            raise ValueError('error_type must be a non-empty string')
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError('message must be a non-empty string')
        object.__setattr__(self, 'candidates', tuple(self.candidates))
        object.__setattr__(self, 'occurred_at', _utc(self.occurred_at))


@dataclass(frozen=True)
class SelfLoopEvaluation:
    internal_inputs: tuple[InternalInputCandidate, ...] = ()
    proactive_task_candidates: tuple[ProactiveTaskCandidate, ...] = ()
    expression_candidates: tuple[ExpressionCandidate, ...] = ()
    failures: tuple[ReviewFailure, ...] = ()
    busy: bool = False
    stale_count: int = 0


class SelfLoopOrchestrator:
    """Accumulate events, review eligible batches, and only emit downstream candidates."""

    def __init__(
        self,
        collector: CandidateCollector,
        review_policy: ReviewPolicy,
        expression_policy: ExpressionPolicy,
        state: SelfLoopState,
        reviewer: ThoughtReviewer,
        *,
        store: StateStore | None = None,
        review_lease_seconds: int = 60,
        expression_reservation_seconds: int = 300,
        utc_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(collector, CandidateCollector):
            raise ValueError('collector must be a CandidateCollector')
        if not isinstance(review_policy, ReviewPolicy):
            raise ValueError('review_policy must be a ReviewPolicy')
        if not isinstance(expression_policy, ExpressionPolicy):
            raise ValueError('expression_policy must be an ExpressionPolicy')
        if not isinstance(state, SelfLoopState):
            raise ValueError('state must be a SelfLoopState')
        if not callable(getattr(reviewer, 'review', None)):
            raise ValueError('reviewer must implement ThoughtReviewer')
        if type(review_lease_seconds) is not int or review_lease_seconds <= 0:
            raise ValueError('review_lease_seconds must be a positive integer')
        if type(expression_reservation_seconds) is not int or expression_reservation_seconds <= 0:
            raise ValueError('expression_reservation_seconds must be a positive integer')
        if store is not None and store is not collector.store:
            raise ValueError('store must be the collector StateStore')
        self.collector = collector
        self.review_policy = review_policy
        self.expression_policy = expression_policy
        self.state = state
        self.reviewer = reviewer
        self.store = store or collector.store
        self.state.bind(self.store)
        self.review_lease = timedelta(seconds=review_lease_seconds)
        self.expression_reservation = timedelta(seconds=expression_reservation_seconds)
        self.utc_clock = utc_clock
        self._owner_id = uuid4().hex
        self._lock = RLock()
        self._review_in_progress = False

    def collect(self, event: SelfLoopCandidate) -> None:
        """Only accumulate; this entry point cannot review, output, or execute."""
        with self._lock:
            self.collector.add(event)

    def evaluate(self, now: datetime, context: Mapping[str, object]) -> SelfLoopEvaluation:
        """Reserve a review slot, then invoke the reviewer without blocking collection."""
        now = _utc(now)
        gaming, do_not_disturb = _activity_flags(context)
        with self._lock:
            self.state._reload()
            if self._review_in_progress:
                return SelfLoopEvaluation(busy=True)
            lease = self.store.acquire_review_lease_token(self._owner_id, now, now + self.review_lease)
            if lease is None:
                return SelfLoopEvaluation(busy=True)
            pending = self.collector.snapshot()
            reviewable = tuple(
                candidate
                for candidate in pending
                if self.review_policy.decide(candidate, self.state, now).eligible
            )
            if not reviewable:
                self.store.release_review_lease_token(lease)
                return SelfLoopEvaluation()
            self._review_in_progress = True
        try:
            reviewed = self.reviewer.review(reviewable, now=now, context=context)
            reviewed = self._normalise_unassigned(tuple(reviewed), reviewable)
            completion_now = _utc(self.utc_clock()) if self.utc_clock is not None else now
            with self._lock:
                if not self.store.verify_review_lease(lease, completion_now):
                    raise RuntimeError('review lease expired')
                self._validate_reviewed(reviewed, reviewable)
                current_reviewed, stale_count = self._current_reviewed(reviewed)
                evaluation, silenced = self._build_candidates(
                    current_reviewed,
                    completion_now,
                    gaming,
                    do_not_disturb,
                    stale_count,
                )
                items, reservations = self._outbox_operations(evaluation, completion_now)
                if not self.store.commit_review(lease, current_reviewed, items, reservations, silenced, completion_now):
                    raise RuntimeError('review commit rejected by lease, candidate CAS, or expression budget')
                self.state._reload()
                # The return value is constructed only after the same transaction persisted its outbox.
                return evaluation
        except Exception as error:
            with self._lock:
                return SelfLoopEvaluation(
                    failures=(ReviewFailure(reviewable, type(error).__name__, _safe_error_message(error), now),),
                )
        finally:
            with self._lock:
                self._review_in_progress = False
            self.store.release_review_lease_token(lease)

    def _current_reviewed(
        self,
        reviewed: tuple[ReviewedThought, ...],
    ) -> tuple[tuple[ReviewedThought, ...], int]:
        """Filter stale model output before it can affect any downstream state."""
        current_by_identity = {
            candidate.identity: candidate
            for candidate in self.collector.snapshot()
        }
        current = tuple(
            thought
            for thought in reviewed
            if current_by_identity.get(thought.candidate.identity) == thought.candidate
        )
        return current, len(reviewed) - len(current)

    @staticmethod
    def _normalise_unassigned(
        reviewed: tuple[ReviewedThought, ...],
        reviewable: tuple[SelfLoopCandidate, ...],
    ) -> tuple[ReviewedThought, ...]:
        """Compatibility for adapters that return the raw, not-yet-stored DTO."""
        current = {candidate.identity: candidate for candidate in reviewable}
        return tuple(
            replace(thought, candidate=current[thought.candidate.identity])
            if thought.candidate.generation == 0 and thought.candidate.sequence == 0
            and thought.candidate.identity in current else thought
            for thought in reviewed
        )

    def ack_expression_delivered(self, candidate: ExpressionCandidate, at: datetime) -> bool:
        """Record expression budget only after the output layer confirms delivery."""
        if not isinstance(candidate, ExpressionCandidate):
            raise ValueError('candidate must be an ExpressionCandidate')
        at = _utc(at)
        with self._lock:
            item = self.store.claim_outbox_one(
                f'expression-{candidate.expression_id}', self._owner_id, at, at + self.expression_reservation,
            )
            return item is not None and self.store.ack_outbox(item, at)

    def release_expression(self, candidate: ExpressionCandidate, now: datetime) -> bool:
        if not isinstance(candidate, ExpressionCandidate):
            raise ValueError('candidate must be an ExpressionCandidate')
        now = _utc(now)
        item = self.store.claim_outbox_one(
            f'expression-{candidate.expression_id}', self._owner_id, now, now + self.expression_reservation,
        )
        return item is not None and self.store.release_outbox(item, now)

    def _build_candidates(
        self,
        reviewed: tuple[ReviewedThought, ...],
        now: datetime,
        gaming: bool,
        do_not_disturb: bool,
        stale_count: int,
    ) -> tuple[SelfLoopEvaluation, tuple[SelfLoopCandidate, ...]]:
        """Build all downstream candidates before committing collector or state changes."""
        internal_inputs: list[InternalInputCandidate] = []
        tasks: list[ProactiveTaskCandidate] = []
        expressions: list[ExpressionCandidate] = []
        silenced: list[SelfLoopCandidate] = []
        try:
            for thought in reviewed:
                if thought.internal_thought is not None:
                    internal_inputs.append(InternalInputCandidate(thought.candidate, thought.internal_thought))
                if thought.proactive_task is not None:
                    tasks.append(ProactiveTaskCandidate(thought.candidate, thought.proactive_task))
                if thought.expression is None:
                    continue
                decision = self.expression_policy.decide(
                    thought.candidate,
                    self.state,
                    now,
                    gaming=gaming,
                    do_not_disturb=do_not_disturb,
                )
                if decision.eligible:
                    # Reservation itself is inserted inside commit_review, together with
                    # output rows and candidate acknowledgements.
                    if len(expressions) + self.state.expressions_today(now) < self.expression_policy.daily_budget:
                        reservation_id = uuid4().hex
                        expressions.append(ExpressionCandidate(
                            thought.candidate,
                            thought.expression,
                            reservation_id=reservation_id,
                        ))
                    else:
                        silenced.append(thought.candidate)
                else:
                    silenced.append(thought.candidate)
        except Exception:
            raise
        return (
            SelfLoopEvaluation(
                tuple(internal_inputs),
                tuple(tasks),
                tuple(expressions),
                stale_count=stale_count,
            ),
            tuple(silenced),
        )

    def _outbox_operations(
        self,
        evaluation: SelfLoopEvaluation,
        now: datetime,
    ) -> tuple[tuple[OutboxItem, ...], tuple[tuple[str, str, str, int, datetime], ...]]:
        """Translate returned views to durable records without performing side effects."""
        items: list[OutboxItem] = []
        for internal in evaluation.internal_inputs:
            items.append(OutboxItem(
                f'internal-{internal.candidate.candidate_id}', 'internal', internal.candidate.candidate_id,
                json.dumps({'content': internal.content, 'role': internal.role, 'source': internal.source}, sort_keys=True),
                'pending', now,
            ))
        for task in evaluation.proactive_task_candidates:
            items.append(OutboxItem(
                f'task-{task.candidate.candidate_id}', 'task', task.candidate.candidate_id,
                json.dumps({'task': task.task, 'source': task.source}, sort_keys=True), 'pending', now,
            ))
        reservations: list[tuple[str, str, str, int, datetime]] = []
        for expression in evaluation.expression_candidates:
            items.append(OutboxItem(
                f'expression-{expression.expression_id}', 'expression', expression.candidate.candidate_id,
                json.dumps({
                    'content': expression.content, 'expression_id': expression.expression_id,
                    'reservation_id': expression.reservation_id, 'topic': expression.candidate.topic,
                    'value': expression.candidate.value,
                }, sort_keys=True), 'pending', now,
            ))
            reservations.append((
                expression.reservation_id, expression.candidate.candidate_id, now.date().isoformat(),
                self.expression_policy.daily_budget, now + self.expression_reservation,
            ))
        return tuple(items), tuple(reservations)

    @staticmethod
    def _validate_reviewed(
        reviewed: tuple[ReviewedThought, ...],
        reviewable: tuple[SelfLoopCandidate, ...],
    ) -> None:
        if len(reviewed) != len(reviewable) or any(not isinstance(item, ReviewedThought) for item in reviewed):
            raise ValueError('reviewer must return ReviewedThought values')
        expected = {candidate.identity for candidate in reviewable}
        actual = [item.candidate.identity for item in reviewed]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise ValueError('reviewer must return exactly one result for each reviewed candidate')


def _activity_flags(context: Mapping[str, object]) -> tuple[bool, bool]:
    if not isinstance(context, Mapping):
        raise ValueError('context must be a mapping')
    gaming = context.get('gaming', False)
    do_not_disturb = context.get('do_not_disturb', False)
    if type(gaming) is not bool or type(do_not_disturb) is not bool:
        raise ValueError('activity context flags must be bool')
    return gaming, do_not_disturb


def _safe_error_message(error: Exception) -> str:
    del error
    return 'review failed; details redacted'


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('now must be an aware UTC datetime')
    return value.astimezone(timezone.utc)
