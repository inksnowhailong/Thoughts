"""Transactional main-agent event runtime.

The runtime deliberately owns no production integrations.  Callers inject one
commit port, so a turn is either delivered as one proposed effect set or not
delivered at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import sqlite3
from threading import Lock
from typing import Callable, Iterable, Mapping, Protocol

from .main_event_parser import EventValidationError, MainEvent, freeze_json, parse_events, parse_jsonl, thaw_json


@dataclass(frozen=True)
class StagedEffect:
    """A requested integration effect, not an executed integration call."""

    kind: str
    payload: Mapping[str, object]
    idempotency_key: str
    event_fingerprint: str


@dataclass(frozen=True)
class TurnOutcome:
    """Stable result of a processed turn, including idempotent replays."""

    correlation_id: str
    effects: tuple[StagedEffect, ...]
    committed: bool | None
    shadow: bool
    repaired: bool = False
    replayed: bool = False
    result: object | None = None
    proposals: tuple[StagedEffect, ...] = ()


@dataclass(frozen=True)
class CommitResult:
    """Outcome supplied by the transactional persistence adapter."""

    created: bool
    result: object


class CommitPort(Protocol):
    """Atomically persists all effects, or returns the prior exact commit."""

    def commit_if_absent(
        self, turn_id: str, fingerprint: str, effects: tuple[StagedEffect, ...]
    ) -> CommitResult: ...


class IdempotencyConflict(ValueError):
    """A turn or event idempotency key was reused for different content."""


class InMemoryCommitPort:
    """Thread-safe test adapter modelling a durable atomic commit boundary."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._turns: dict[str, tuple[str, object]] = {}
        self._events: dict[str, str] = {}
        self.commits: list[tuple[StagedEffect, ...]] = []

    def commit_if_absent(
        self, turn_id: str, fingerprint: str, effects: tuple[StagedEffect, ...]
    ) -> CommitResult:
        with self._lock:
            prior = self._turns.get(turn_id)
            if prior is not None:
                if prior[0] != fingerprint:
                    raise IdempotencyConflict('turn id was reused with different content')
                return CommitResult(created=False, result=prior[1])
            for effect in effects:
                event_prior = self._events.get(effect.idempotency_key)
                if event_prior is not None and event_prior != effect.event_fingerprint:
                    raise IdempotencyConflict('idempotency key was reused with different event content')
                if event_prior is not None:
                    raise IdempotencyConflict('idempotency key belongs to another turn')
            result = freeze_json({'turn_id': turn_id, 'effect_count': len(effects)})
            self._turns[turn_id] = (fingerprint, result)
            for effect in effects:
                self._events[effect.idempotency_key] = effect.event_fingerprint
            self.commits.append(effects)
            return CommitResult(created=True, result=result)


class SQLiteCommitPort:
    """Durable candidate-outbox commit; it never executes effect handlers."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = Lock()
        self._db.executescript(
            'CREATE TABLE IF NOT EXISTS main_turns (turn_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);'
            'CREATE TABLE IF NOT EXISTS main_event_outbox (idempotency_key TEXT PRIMARY KEY, event_fingerprint TEXT NOT NULL, turn_id TEXT NOT NULL, effect TEXT NOT NULL);'
        )

    def commit_if_absent(self, turn_id: str, fingerprint: str, effects: tuple[StagedEffect, ...]) -> CommitResult:
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                prior = self._db.execute('SELECT fingerprint, result FROM main_turns WHERE turn_id=?', (turn_id,)).fetchone()
                if prior is not None:
                    if prior[0] != fingerprint:
                        raise IdempotencyConflict('turn id was reused with different content')
                    self._db.execute('COMMIT')
                    return CommitResult(False, freeze_json(json.loads(prior[1])))
                for effect in effects:
                    prior_event = self._db.execute('SELECT event_fingerprint FROM main_event_outbox WHERE idempotency_key=?', (effect.idempotency_key,)).fetchone()
                    if prior_event is not None:
                        raise IdempotencyConflict('event idempotency key was already used')
                result = {'turn_id': turn_id, 'effect_count': len(effects)}
                self._db.execute('INSERT INTO main_turns VALUES (?, ?, ?)', (turn_id, fingerprint, json.dumps(result)))
                for effect in effects:
                    body = {'kind': effect.kind, 'payload': thaw_json(effect.payload)}
                    self._db.execute('INSERT INTO main_event_outbox VALUES (?, ?, ?, ?)', (effect.idempotency_key, effect.event_fingerprint, turn_id, json.dumps(body, sort_keys=True)))
                self._db.execute('COMMIT')
                return CommitResult(True, freeze_json(result))
            except Exception:
                self._db.execute('ROLLBACK')
                raise


Repairer = Callable[[object], object]


class MainAgentRuntime:
    """Stages a complete turn before one atomic integration-port invocation."""

    def __init__(
        self,
        *,
        commit: CommitPort,
        repairer: Repairer | None = None,
        shadow: bool = False,
    ) -> None:
        self._commit_port = commit
        self._repairer = repairer
        self._shadow = shadow

    def process_jsonl(self, source: str | bytes) -> TurnOutcome:
        """Accept JSONL, optionally repair it once, then process it."""
        return self._with_one_repair(source, lambda item: parse_jsonl(item))

    def process_events(self, source: Iterable[Mapping[str, object]]) -> TurnOutcome:
        """Accept a structured batch, optionally repair it once."""
        values = tuple(source)
        return self._with_one_repair(values, lambda item: parse_events(item))

    def _with_one_repair(
        self, source: object, parser: Callable[[object], tuple[MainEvent, ...]]
    ) -> TurnOutcome:
        try:
            events = parser(source)
            effects = self._stage(events)
        except EventValidationError:
            if self._repairer is None:
                raise
            repaired_source = self._repairer(source)
            try:
                events = parser(repaired_source)
                effects = self._stage(events)
            except EventValidationError as error:
                raise EventValidationError('event batch rejected after one repair attempt') from error
            return self._commit(events, effects, repaired=True)
        return self._commit(events, effects, repaired=False)

    def _commit(
        self, events: tuple[MainEvent, ...], effects: tuple[StagedEffect, ...], *, repaired: bool
    ) -> TurnOutcome:
        fingerprint = self._fingerprint(events)
        turn_id = str(events[-1].payload['turn_id'])
        if self._shadow:
            proposals = tuple(effect for effect in effects if effect.kind != 'session.write')
            return TurnOutcome(events[0].correlation_id, (), None, True, repaired=repaired, proposals=proposals)
        committed = self._commit_port.commit_if_absent(turn_id, fingerprint, effects)
        return TurnOutcome(
            events[0].correlation_id,
            effects,
            True,
            False,
            repaired=repaired,
            replayed=not committed.created,
            result=committed.result,
        )

    @staticmethod
    def _stage(events: tuple[MainEvent, ...]) -> tuple[StagedEffect, ...]:
        effects: list[StagedEffect] = []
        for event in events:
            if event.type != 'turn.completed':
                effects.append(StagedEffect(
                    event.type, event.payload, event.idempotency_key, MainAgentRuntime._fingerprint((event,))
                ))
        completed = events[-1]
        effects.append(StagedEffect(
            'session.write',
            {'correlation_id': events[0].correlation_id},
            completed.idempotency_key,
            MainAgentRuntime._fingerprint((completed,)),
        ))
        return tuple(effects)

    @staticmethod
    def _fingerprint(events: tuple[MainEvent, ...]) -> str:
        body = [
            {
                'version': event.version, 'idempotency_key': event.idempotency_key,
                'type': event.type, 'payload': thaw_json(event.payload),
            }
            for event in events
        ]
        return sha256(json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
