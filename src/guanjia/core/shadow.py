"""Latest-wins shadow coordinator for complete turns.

Coordinates low-priority shadow evaluations behind GUANJIA_CORE_SHADOW_V1:
- Only complete accepted turns enter the coordinator.
- Barge-in or a newer accepted input immediately cancels in-flight/pending work.
- Stale generations are fenced and never counted as completed shadow outcomes.
- Exposes narrow read-only metrics and allowlisted difference field names for Panel.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable, Iterable

from guanjia.input.adapters.contracts import ShadowInputRecord

from .agent_runtime import TurnOutcome
from .main_program import (
    CancellationToken,
    CompleteTurnGenerator,
    MainProgram,
    ShadowTurnCancelledError,
)


ALLOWED_CORE_DIFFERENCE_FIELDS: frozenset[str] = frozenset({
    'reply_intent',
    'reply_present',
    'task_create_count',
    'task_control_count',
    'tool_read_count',
    'output_request_count',
    'memory_request_count',
})


@dataclass(frozen=True)
class CoreShadowMetrics:
    """Narrow read-only metrics safe for Panel projection."""

    queue: int
    accepted: int
    dropped: int
    failures: int
    cancelled: int
    completed: int
    difference_fields: tuple[str, ...] = ()


class CoreShadowCoordinator:
    """Single-worker latest-wins shadow runner with generation fencing."""

    def __init__(
        self,
        program: MainProgram | None = None,
        *,
        generator: CompleteTurnGenerator | None = None,
        on_outcome: Callable[[ShadowInputRecord, TurnOutcome], None] | None = None,
    ) -> None:
        self._program = program or MainProgram(generator=generator)
        self._on_outcome = on_outcome
        self._condition = threading.Condition()
        self._closed = False
        self._generation = 0
        self._pending: tuple[int, ShadowInputRecord, CancellationToken] | None = None
        self._active_token: CancellationToken | None = None
        self._active_generation: int | None = None
        self._accepted = 0
        self._dropped = 0
        self._failures = 0
        self._cancelled = 0
        self._completed = 0
        self._difference_fields: tuple[str, ...] = ()
        self._worker = threading.Thread(
            target=self._run_loop,
            name='CoreShadowCoordinator-worker',
            daemon=True,
        )
        self._worker.start()

    def observe(self, record: ShadowInputRecord) -> bool:
        """Accept a complete input turn, cancelling any older low-priority shadow turn."""
        if not isinstance(record, ShadowInputRecord):
            raise TypeError('record must be a ShadowInputRecord')
        if not record.text.strip():
            return False
        token_to_cancel: CancellationToken | None = None
        pending_to_cancel: CancellationToken | None = None
        with self._condition:
            if self._closed:
                self._dropped += 1
                return False
            self._generation += 1
            gen = self._generation
            self._accepted += 1
            if self._pending is not None:
                _, _, pending_to_cancel = self._pending
                self._dropped += 1
                self._cancelled += 1
            if self._active_token is not None:
                token_to_cancel = self._active_token
            self._pending = (gen, record, CancellationToken())
            self._condition.notify_all()
        if pending_to_cancel is not None:
            pending_to_cancel.cancel('superseded_pending')
        if token_to_cancel is not None:
            token_to_cancel.cancel('superseded_by_new_input')
        return True

    def cancel_current(self, reason: str = 'interrupted') -> bool:
        """Cancel any queued or active shadow evaluation without touching production Brain."""
        pending_token: CancellationToken | None = None
        active_token: CancellationToken | None = None
        with self._condition:
            self._generation += 1
            if self._pending is not None:
                _, _, pending_token = self._pending
                self._pending = None
                self._dropped += 1
                self._cancelled += 1
            if self._active_token is not None:
                active_token = self._active_token
            self._condition.notify_all()
        if pending_token is not None:
            pending_token.cancel(reason)
        if active_token is not None:
            active_token.cancel(reason)
        return pending_token is not None or active_token is not None

    def record_difference_fields(self, fields: Iterable[str]) -> tuple[str, ...]:
        """Store only allowlisted field names for read-only Panel diagnostics."""
        filtered = tuple(
            sorted({
                field
                for field in fields
                if isinstance(field, str) and field in ALLOWED_CORE_DIFFERENCE_FIELDS
            })
        )
        with self._condition:
            self._difference_fields = filtered
            return filtered

    def compare_with_legacy(
        self,
        outcome: TurnOutcome,
        *,
        legacy_replied: bool,
        legacy_task_creates: int = 0,
        legacy_output_requests: int = 0,
    ) -> tuple[str, ...]:
        """Compute allowlisted difference field names from a shadow TurnOutcome."""
        proposals = outcome.proposals
        shadow_replied = any(item.kind == 'reply.delta' for item in proposals)
        shadow_tasks = sum(1 for item in proposals if item.kind == 'task.create')
        shadow_outputs = sum(1 for item in proposals if item.kind == 'output.request')
        diffs: list[str] = []
        if shadow_replied != bool(legacy_replied):
            diffs.append('reply_present')
        if shadow_tasks != int(legacy_task_creates):
            diffs.append('task_create_count')
        if shadow_outputs != int(legacy_output_requests):
            diffs.append('output_request_count')
        return self.record_difference_fields(diffs)

    def wait_idle(self, *, timeout: float = 1.0) -> bool:
        """Wait until no pending or active shadow turn remains."""
        with self._condition:
            return self._condition.wait_for(
                lambda: self._pending is None and self._active_token is None,
                timeout=max(0.0, float(timeout)),
            )

    def metrics(self) -> CoreShadowMetrics:
        with self._condition:
            queue_depth = (1 if self._pending is not None else 0) + (
                1 if self._active_token is not None else 0
            )
            return CoreShadowMetrics(
                queue=queue_depth,
                accepted=self._accepted,
                dropped=self._dropped,
                failures=self._failures,
                cancelled=self._cancelled,
                completed=self._completed,
                difference_fields=self._difference_fields,
            )

    def close(self, *, timeout: float = 0.2) -> bool:
        with self._condition:
            self._closed = True
        self.cancel_current('closed')
        with self._condition:
            self._condition.notify_all()
        self._worker.join(max(0.0, float(timeout)))
        return not self._worker.is_alive()

    def _run_loop(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed and self._pending is None:
                    return
                assert self._pending is not None
                gen, record, token = self._pending
                self._pending = None
                self._active_token = token
                self._active_generation = gen
            try:
                outcome = self._program.run_shadow_turn(
                    input_id=str(record.input_id),
                    correlation_id=str(record.correlation_id),
                    text=record.text,
                    cancellation=token,
                    metadata=dict(record.metadata),
                )
            except ShadowTurnCancelledError:
                with self._condition:
                    self._cancelled += 1
                    if self._active_token is token:
                        self._active_token = None
                        self._active_generation = None
                    self._condition.notify_all()
                continue
            except Exception:  # noqa: BLE001
                with self._condition:
                    if token.cancelled or gen != self._generation:
                        self._cancelled += 1
                    else:
                        self._failures += 1
                    if self._active_token is token:
                        self._active_token = None
                        self._active_generation = None
                    self._condition.notify_all()
                continue

            deliver_callback = False
            with self._condition:
                if token.cancelled or gen != self._generation:
                    self._cancelled += 1
                else:
                    self._completed += 1
                    deliver_callback = True
                if self._active_token is token:
                    self._active_token = None
                    self._active_generation = None
                self._condition.notify_all()

            if deliver_callback and self._on_outcome is not None:
                try:
                    self._on_outcome(record, outcome)
                except Exception:  # noqa: BLE001
                    pass
