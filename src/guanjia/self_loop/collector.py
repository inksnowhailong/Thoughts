"""Candidate accumulation and deterministic de-duplication."""

from __future__ import annotations

from threading import RLock

from .contracts import SelfLoopCandidate


class CandidateCollector:
    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], SelfLoopCandidate] = {}
        self._lock = RLock()

    def add(self, candidate: SelfLoopCandidate) -> None:
        """Keep the highest-value instance of a same-topic/same-summary candidate."""
        with self._lock:
            existing = self._pending.get(candidate.identity)
            if existing is None or _priority(candidate) > _priority(existing):
                self._pending[candidate.identity] = candidate

    def snapshot(self) -> tuple[SelfLoopCandidate, ...]:
        """Return a stable batch without consuming candidates."""
        with self._lock:
            return tuple(sorted(self._pending.values(), key=_priority, reverse=True))

    def drain(self) -> tuple[SelfLoopCandidate, ...]:
        """Compatibility alias: inspect candidates; explicit ack removes them."""
        return self.snapshot()

    def ack(self, candidate: SelfLoopCandidate) -> bool:
        return self.remove(candidate.identity)

    def remove(self, identity: tuple[str, str]) -> bool:
        with self._lock:
            return self._pending.pop(identity, None) is not None

    def requeue(self, candidate: SelfLoopCandidate) -> None:
        self.add(candidate)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


def _priority(candidate: SelfLoopCandidate) -> tuple[float, float, float, float]:
    return candidate.urgency, candidate.value, candidate.freshness, -candidate.interruption_cost
