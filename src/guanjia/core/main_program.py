"""Complete-turn main program boundary for shadow and controlled evaluation.

This module enforces three invariants required by Wave 3 Task 5:
1. Only complete turns are evaluated; cancellation before or during generation
   discards the entire response before parsing.
2. Shadow runtime never commits effects, never reuses MAIN_SESSION_KEY, never
   creates real tasks, and never invokes output channels.
3. Model selection always flows through ModelPolicy rather than hardcoded model
   names in business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from .agent_runtime import CommitPort, CommitResult, MainAgentRuntime, StagedEffect, TurnOutcome
from .model_policy import ModelPolicy, ModelProfile


DEFAULT_SHADOW_PROFILES: tuple[ModelProfile, ...] = (
    ModelProfile(
        name='glm-4-flash',
        roles=('main', 'worker'),
        capabilities=('jsonl', 'chat'),
        cost=0,
        free_tokens=1_000_000,
    ),
    ModelProfile(
        name='deepseek-v4-flash',
        roles=('main', 'worker', 'memory'),
        capabilities=('jsonl', 'chat', 'reasoning'),
        cost=1,
    ),
)

FORBIDDEN_SESSION_KEYS = frozenset({'agent:main:main', 'main', 'agent:main'})


class ShadowCommitForbiddenError(RuntimeError):
    """Raised if shadow execution ever attempts to invoke CommitPort."""


class ShadowTurnCancelledError(RuntimeError):
    """Raised when a low-priority shadow turn is cancelled by new input or barge-in."""


class _FailClosedShadowCommitPort(CommitPort):
    """Defense-in-depth commit port that hard-fails if called."""

    def commit_if_absent(
        self, turn_id: str, fingerprint: str, effects: tuple[StagedEffect, ...]
    ) -> CommitResult:
        raise ShadowCommitForbiddenError('MainAgentRuntime(shadow=True) must never commit')


class CancellationToken:
    """Thread-safe cancellation token with optional transport abort callbacks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cancelled = False
        self._reason: str | None = None
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = 'cancelled') -> None:
        if not isinstance(reason, str) or not reason.strip():
            reason = 'cancelled'
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason.strip()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass

    def on_cancel(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._cancelled:
                run_now = True
            else:
                self._callbacks.append(callback)
                run_now = False
        if run_now:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass

    def raise_if_cancelled(self) -> None:
        with self._lock:
            if self._cancelled:
                raise ShadowTurnCancelledError(self._reason or 'cancelled')


@dataclass(frozen=True)
class ShadowTurnRequest:
    """Immutable complete-turn input for a shadow main-program evaluation."""

    input_id: str
    correlation_id: str
    session_key: str
    model: str
    text: str
    tools_disabled: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.input_id, str) or not self.input_id.strip():
            raise ValueError('input_id must be a non-empty string')
        if not isinstance(self.correlation_id, str) or not self.correlation_id.strip():
            raise ValueError('correlation_id must be a non-empty string')
        if not isinstance(self.session_key, str) or not self.session_key.strip():
            raise ValueError('session_key must be a non-empty string')
        if self.session_key in FORBIDDEN_SESSION_KEYS or not self.session_key.startswith('agent:shadow:'):
            raise ValueError('shadow turn must use an isolated agent:shadow:<id> session_key')
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError('model must be a non-empty string')
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError('text must be a non-empty complete turn')
        if self.tools_disabled is not True:
            raise ValueError('shadow generation requires tools_disabled=True')


class CompleteTurnGenerator(Protocol):
    """Port that produces complete JSONL for one turn without executing tools."""

    def generate(self, request: ShadowTurnRequest, cancellation: CancellationToken) -> str | bytes: ...


class FailClosedTurnGenerator:
    """Default generator used when no external tool-disabled transport is wired."""

    def generate(self, request: ShadowTurnRequest, cancellation: CancellationToken) -> str | bytes:
        cancellation.raise_if_cancelled()
        raise RuntimeError('shadow turn generator is not configured')


class MainProgram:
    """Evaluates complete turns in shadow mode without commits, tasks, or output."""

    def __init__(
        self,
        *,
        generator: CompleteTurnGenerator | None = None,
        model_policy: ModelPolicy | None = None,
        required_capabilities: tuple[str, ...] = ('jsonl',),
    ) -> None:
        self._generator = generator or FailClosedTurnGenerator()
        self._model_policy = model_policy or ModelPolicy(DEFAULT_SHADOW_PROFILES)
        self._required_capabilities = required_capabilities
        self._runtime = MainAgentRuntime(
            commit=_FailClosedShadowCommitPort(),
            repairer=None,
            shadow=True,
        )

    @staticmethod
    def allocate_shadow_session_key() -> str:
        """Allocate a fresh non-reused shadow session key isolated from MAIN_SESSION_KEY."""
        return f'agent:shadow:{uuid4().hex}'

    def run_shadow_turn(
        self,
        *,
        input_id: str,
        correlation_id: str,
        text: str,
        cancellation: CancellationToken | None = None,
        estimated_tokens: int = 256,
        metadata: Mapping[str, object] | None = None,
    ) -> TurnOutcome:
        """Run a complete turn in shadow mode and return proposals only."""
        token = cancellation or CancellationToken()
        token.raise_if_cancelled()
        selection = self._model_policy.select(
            'main',
            required_capabilities=self._required_capabilities,
            estimated_tokens=max(1, int(estimated_tokens)),
        )
        session_key = self.allocate_shadow_session_key()
        request = ShadowTurnRequest(
            input_id=input_id,
            correlation_id=correlation_id,
            session_key=session_key,
            model=selection.name,
            text=text,
            tools_disabled=True,
            metadata=dict(metadata or {}),
        )
        raw_jsonl = self._generator.generate(request, token)
        token.raise_if_cancelled()
        outcome = self._runtime.process_jsonl(raw_jsonl)
        token.raise_if_cancelled()
        if not outcome.shadow or outcome.committed is not None or outcome.effects:
            raise ShadowCommitForbiddenError('shadow runtime produced non-shadow effects')
        return outcome


class ProductionMainProgram:
    """Production counterpart of MainProgram that commits real effects."""

    def __init__(self, *, commit: CommitPort, repairer: object | None = None) -> None:
        self._runtime = MainAgentRuntime(commit=commit, repairer=repairer, shadow=False)

    def process_events(self, events: object) -> TurnOutcome:
        """Commit a complete structured event batch to the production commit port."""
        return self._runtime.process_events(events)  # type: ignore[arg-type]

    def process_jsonl(self, source: str | bytes) -> TurnOutcome:
        """Commit complete JSONL emitted by a production generator."""
        return self._runtime.process_jsonl(source)

