"""Production effect commit helpers for the modular main runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock
from typing import Mapping, Protocol

from .agent_runtime import CommitPort, CommitResult, StagedEffect


class EffectHandler(Protocol):
    """Executes one committed staged effect."""

    def __call__(self, effect: StagedEffect) -> object: ...


@dataclass(frozen=True)
class EffectReceipt:
    """Durable evidence that one effect reached its production handler."""

    idempotency_key: str
    kind: str
    ok: bool
    result: object | None = None
    error: str | None = None


class JsonlEffectSink:
    """Append-only sink used by production cutover to make effects auditable."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def record(self, effect: StagedEffect, receipt: EffectReceipt) -> None:
        row = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'kind': effect.kind,
            'idempotency_key': effect.idempotency_key,
            'event_fingerprint': effect.event_fingerprint,
            'payload': dict(effect.payload),
            'receipt': {'ok': receipt.ok, 'result': receipt.result, 'error': receipt.error},
        }
        with self._lock:
            with self._path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')


class EffectRouter:
    """Routes committed effects to production handlers, fail-closed by default."""

    def __init__(self, handlers: Mapping[str, EffectHandler] | None = None) -> None:
        self._handlers = dict(handlers or {})

    def execute(self, effect: StagedEffect) -> EffectReceipt:
        handler = self._handlers.get(effect.kind)
        if handler is None:
            return EffectReceipt(effect.idempotency_key, effect.kind, False, error=f'no handler for {effect.kind}')
        try:
            return EffectReceipt(effect.idempotency_key, effect.kind, True, result=handler(effect))
        except Exception as exc:  # noqa: BLE001
            return EffectReceipt(effect.idempotency_key, effect.kind, False, error=f'{type(exc).__name__}: {exc}')


class ExecutingCommitPort:
    """Persists a turn once, then executes each newly-created effect exactly once."""

    def __init__(self, commit: CommitPort, router: EffectRouter, sink: JsonlEffectSink | None = None) -> None:
        self._commit = commit
        self._router = router
        self._sink = sink

    def commit_if_absent(self, turn_id: str, fingerprint: str, effects: tuple[StagedEffect, ...]) -> CommitResult:
        committed = self._commit.commit_if_absent(turn_id, fingerprint, effects)
        if not committed.created:
            return committed
        receipts = []
        for effect in effects:
            receipt = self._router.execute(effect)
            receipts.append(receipt)
            if self._sink is not None:
                self._sink.record(effect, receipt)
            if not receipt.ok:
                raise RuntimeError(receipt.error or f'effect failed: {effect.kind}')
        return CommitResult(True, {'commit': committed.result, 'effects': [r.__dict__ for r in receipts]})


__all__ = ['EffectReceipt', 'EffectRouter', 'ExecutingCommitPort', 'JsonlEffectSink']
