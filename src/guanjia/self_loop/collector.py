"""Candidate accumulation and deterministic de-duplication."""

from __future__ import annotations

from .contracts import SelfLoopCandidate
from .store import InMemoryStateStore, StateStore


class CandidateCollector:
    def __init__(self, store: StateStore | None = None) -> None:
        self._store = store or InMemoryStateStore()

    @property
    def store(self) -> StateStore:
        return self._store

    def add(self, candidate: SelfLoopCandidate) -> SelfLoopCandidate:
        """Keep the highest-value instance of a same-topic/same-summary candidate."""
        if not isinstance(candidate, SelfLoopCandidate):
            raise ValueError('candidate must be a SelfLoopCandidate')
        self._store.add_candidate(candidate)
        current = next(item for item in self._store.snapshot_candidates() if item.identity == candidate.identity)
        return current

    def snapshot(self) -> tuple[SelfLoopCandidate, ...]:
        """Return a stable batch without consuming candidates."""
        return self._store.snapshot_candidates()

    def drain(self) -> tuple[SelfLoopCandidate, ...]:
        """Compatibility alias: inspect candidates; explicit ack removes them."""
        return self.snapshot()

    def ack(self, candidate: SelfLoopCandidate) -> bool:
        return self.remove(candidate)

    def remove(self, candidate: SelfLoopCandidate) -> bool:
        """Remove only if the snapshot candidate is still the current value."""
        if not isinstance(candidate, SelfLoopCandidate):
            raise ValueError('candidate must be a SelfLoopCandidate')
        return self._store.ack_candidate(candidate)

    def requeue(self, candidate: SelfLoopCandidate) -> SelfLoopCandidate:
        return self.add(candidate)

    def __len__(self) -> int:
        return len(self.snapshot())


def _priority(candidate: SelfLoopCandidate) -> tuple[float, float, float, float]:
    return candidate.urgency, candidate.value, candidate.freshness, -candidate.interruption_cost
