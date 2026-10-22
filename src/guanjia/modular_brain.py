"""Modular main-session bridge used during hard cutover.

This class keeps the mature low-latency OpenClaw streaming client but records
each completed voice turn through the production `core` runtime instead of the
old shadow-only path.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from guanjia.brain import Brain
from guanjia.core.agent_runtime import SQLiteCommitPort, StagedEffect
from guanjia.core.effects import EffectRouter, ExecutingCommitPort, JsonlEffectSink
from guanjia.core.main_program import ProductionMainProgram


def _root() -> Path:
    return Path(os.environ.get('GUANJIA_ROOT', r'F:\coding\guanjia-v2-development'))


def _event(event_type: str, correlation_id: str, suffix: str, payload: dict[str, object]) -> dict[str, object]:
    key = f'{correlation_id}:{suffix}'
    return {
        'version': '1.0',
        'event_id': key,
        'correlation_id': correlation_id,
        'idempotency_key': key,
        'type': event_type,
        'payload': payload,
    }


class ModularBrain:
    """Streaming Brain facade backed by a production modular commit ledger."""

    def __init__(self, legacy: Brain | None = None, *, fail_closed: bool | None = None) -> None:
        root = _root()
        data_dir = root / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)
        sink = JsonlEffectSink(data_dir / 'modular-effects.jsonl')
        router = EffectRouter({
            'reply.delta': self._record_effect,
            'output.request': self._record_effect,
            'session.write': self._record_effect,
            'reply.silent': self._record_effect,
            'task.create': self._record_effect,
            'task.control': self._record_effect,
            'tool.read': self._record_effect,
            'memory.request': self._record_effect,
        })
        commit = ExecutingCommitPort(SQLiteCommitPort(str(data_dir / 'modular-main.sqlite')), router, sink)
        self._program = ProductionMainProgram(commit=commit)
        self._legacy = legacy or Brain(session_key='agent:main:main')
        self._fail_closed = (
            os.environ.get('GUANJIA_MODULAR_FAIL_CLOSED', '0').lower() in {'1', 'true', 'yes', 'on'}
            if fail_closed is None else fail_closed
        )
        self.last_run_id: str | None = None
        self.last_meta: dict = {}

    def stream(self, text: str, system: str | None = None) -> Iterator[str]:
        chunks: list[str] = []
        try:
            for chunk in self._legacy.stream(text, system):
                chunks.append(chunk)
                yield chunk
        finally:
            self.last_run_id = getattr(self._legacy, 'last_run_id', None)
            self.last_meta = dict(getattr(self._legacy, 'last_meta', None) or {})
            reply = ''.join(chunks)
            if reply:
                self._commit_turn(text, reply)

    def ask(self, text: str, system: str | None = None) -> str:
        reply = self._legacy.ask(text, system)
        if reply:
            self._commit_turn(text, reply)
        return reply

    def status(self) -> dict[str, object]:
        root = _root()
        effect_log = root / 'data' / 'modular-effects.jsonl'
        return {
            'enabled': True,
            'root': str(root),
            'effect_log': str(effect_log),
            'effect_count': sum(1 for _ in effect_log.open('r', encoding='utf-8')) if effect_log.exists() else 0,
            'last_run_id': self.last_run_id,
            'last_meta': dict(self.last_meta),
        }

    def abort(self) -> None:
        self._legacy.abort()

    def close(self) -> None:
        self._legacy.close()

    def _commit_turn(self, text: str, reply: str) -> None:
        correlation_id = f'turn:{uuid4().hex}'
        turn_id = f'turn:{uuid4().hex}'
        events = [
            _event('reply.delta', correlation_id, 'reply', {'text': reply}),
            _event('output.request', correlation_id, 'voice-output', {'channel': 'voice', 'text': reply}),
            _event('turn.completed', correlation_id, 'completed', {'turn_id': turn_id}),
        ]
        try:
            self._program.process_events(events)
        except Exception:
            if self._fail_closed:
                raise

    @staticmethod
    def _record_effect(effect: StagedEffect) -> dict[str, object]:
        return {
            'kind': effect.kind,
            'idempotency_key': effect.idempotency_key,
            'committed_at': datetime.now(timezone.utc).isoformat(),
        }


__all__ = ['ModularBrain']
