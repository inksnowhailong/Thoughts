"""The durable, transactional source of truth for the self-loop."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from .contracts import SelfLoopCandidate


class IdempotencyConflict(ValueError):
    """A caller reused an idempotency key for a different semantic operation."""

@dataclass(frozen=True)
class ReviewLease:
    owner: str
    token: str
    fencing: int
    expires_at: datetime


@dataclass(frozen=True)
class StoredState:
    """Serializable state which policies must consult instead of process memory."""
    attention: dict[str, tuple[float, datetime]]
    unresolved_ideas: dict[str, datetime]
    silenced: dict[str, datetime]
    waiting_conditions: dict[str, datetime]
    expressions: tuple[tuple[str, datetime], ...]
    last_review_at: datetime | None

@dataclass(frozen=True)
class OutboxItem:
    id: str
    kind: str
    candidate_id: str
    payload: str
    status: str
    created_at: datetime
    owner: str | None = None
    fencing: int = 0
    lease_expires_at: datetime | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (self.id, self.kind, self.candidate_id, self.payload, self.status)):
            raise ValueError('outbox string fields must be non-empty')
        object.__setattr__(self, 'created_at', _utc(self.created_at))


class StateStore(Protocol):
    def add_candidate(self, candidate: SelfLoopCandidate) -> SelfLoopCandidate: ...
    def snapshot_candidates(self) -> tuple[SelfLoopCandidate, ...]: ...
    def ack_candidate(self, candidate: SelfLoopCandidate) -> bool: ...
    def acquire_review_lease_token(self, owner: str, now: datetime, expires_at: datetime) -> ReviewLease | None: ...
    def renew_review_lease(self, lease: ReviewLease, now: datetime, expires_at: datetime) -> ReviewLease | None: ...
    def verify_review_lease(self, lease: ReviewLease, now: datetime) -> bool: ...
    def release_review_lease_token(self, lease: ReviewLease) -> None: ...
    def load_state(self) -> StoredState: ...
    def mutate_state(self, mutator: Any) -> StoredState: ...
    def commit_review(self, lease: ReviewLease, reviewed: tuple[Any, ...], items: tuple[OutboxItem, ...],
                      reservations: tuple[tuple[str, str, str, int, datetime], ...],
                      silenced: tuple[SelfLoopCandidate, ...], at: datetime) -> bool: ...
    def claim_outbox(self, owner: str, now: datetime, expires_at: datetime, *, limit: int = 1) -> tuple[OutboxItem, ...]: ...
    def claim_outbox_one(self, item_id: str, owner: str, now: datetime, expires_at: datetime) -> OutboxItem | None: ...
    def ack_outbox(self, item: OutboxItem, now: datetime) -> bool: ...
    def release_outbox(self, item: OutboxItem, now: datetime) -> bool: ...
    def fail_outbox(self, item: OutboxItem, now: datetime) -> bool: ...


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('timestamps must be aware UTC datetimes')
    return value.astimezone(timezone.utc)


class InMemoryStateStore:
    def __init__(self) -> None:
        self._candidates: dict[tuple[str, str], SelfLoopCandidate] = {}
        self._lease_token: ReviewLease | None = None
        self._fencing = 0
        self._generation = 0
        self._sequence = 0
        self._reservations: dict[str, tuple[str, str, datetime]] = {}
        self._committed: dict[str, str] = {}
        self._outbox: dict[str, OutboxItem] = {}
        self._state = StoredState({}, {}, {}, {}, (), None)
        self._lock = RLock()

    def add_candidate(self, candidate: SelfLoopCandidate) -> SelfLoopCandidate:
        with self._lock:
            existing = self._candidates.get(candidate.identity)
            if existing is not None and _priority(candidate) <= _priority(existing):
                return existing
            self._generation += 1
            self._sequence += 1
            stored = replace(candidate, candidate_id=uuid4().hex, generation=self._generation, sequence=self._sequence)
            self._candidates[candidate.identity] = stored
            return stored

    def snapshot_candidates(self) -> tuple[SelfLoopCandidate, ...]:
        with self._lock:
            return tuple(sorted(self._candidates.values(), key=_priority, reverse=True))

    def ack_candidate(self, candidate: SelfLoopCandidate) -> bool:
        with self._lock:
            if self._candidates.get(candidate.identity) != candidate:
                return False
            del self._candidates[candidate.identity]
            return True

    def acquire_review_lease(self, owner: str, now: datetime, expires_at: datetime) -> bool:
        return self.acquire_review_lease_token(owner, now, expires_at) is not None

    def release_review_lease(self, owner: str) -> None:
        with self._lock:
            if self._lease_token is not None and self._lease_token.owner == owner:
                self._lease_token = None

    def acquire_review_lease_token(self, owner: str, now: datetime, expires_at: datetime) -> ReviewLease | None:
        now, expires_at = _utc(now), _utc(expires_at)
        with self._lock:
            if self._lease_token is not None and self._lease_token.expires_at > now and self._lease_token.owner != owner:
                return None
            self._fencing += 1
            self._lease_token = ReviewLease(owner, str(self._fencing), self._fencing, expires_at)
            return self._lease_token

    def verify_review_lease(self, lease: ReviewLease, now: datetime) -> bool:
        now = _utc(now)
        with self._lock:
            return self._lease_token == lease and lease.expires_at > now

    def renew_review_lease(self, lease: ReviewLease, now: datetime, expires_at: datetime) -> ReviewLease | None:
        now, expires_at = _utc(now), _utc(expires_at)
        with self._lock:
            if self._lease_token != lease or lease.expires_at <= now:
                return None
            renewed = ReviewLease(lease.owner, lease.token, lease.fencing, expires_at)
            self._lease_token = renewed
            return renewed

    def release_review_lease_token(self, lease: ReviewLease) -> None:
        with self._lock:
            if self._lease_token == lease:
                self._lease_token = None

    def reserve_expression(self, reservation_id: str, candidate_id: str, day: str, budget: int, now: datetime, expires_at: datetime) -> bool:
        now, expires_at = _utc(now), _utc(expires_at)
        if type(budget) is not int or budget < 0:
            raise ValueError('budget must be a non-negative integer')
        with self._lock:
            self._reservations = {key: value for key, value in self._reservations.items() if value[2] > now}
            if reservation_id in self._reservations:
                existing = self._reservations[reservation_id]
                if existing != (candidate_id, day, expires_at):
                    raise IdempotencyConflict('reservation_id reused with different reservation')
                return True
            used = sum(1 for _, item_day, _ in self._reservations.values() if item_day == day)
            used += sum(1 for item_day in self._committed.values() if item_day == day)
            if used >= budget:
                return False
            self._reservations[reservation_id] = candidate_id, day, expires_at
            return True

    def commit_expression(self, reservation_id: str, expression_id: str, at: datetime) -> bool:
        at = _utc(at)
        with self._lock:
            if expression_id in self._committed:
                return False
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None or reservation[2] <= at:
                return False
            self._committed[expression_id] = reservation[1]
            return True

    def release_expression(self, reservation_id: str, now: datetime) -> bool:
        _utc(now)
        with self._lock:
            return self._reservations.pop(reservation_id, None) is not None

    def put_outbox(self, item: OutboxItem) -> OutboxItem:
        with self._lock:
            existing = self._outbox.get(item.id)
            if existing is not None and existing != item:
                raise IdempotencyConflict('outbox id reused with different payload')
            self._outbox[item.id] = item
            return item

    def outbox(self) -> tuple[OutboxItem, ...]:
        with self._lock:
            return tuple(self._outbox.values())

    def load_state(self) -> StoredState:
        with self._lock:
            return self._state

    def save_state(self, state: StoredState) -> None:
        with self._lock:
            self._state = state

    def mutate_state(self, mutator: Any) -> StoredState:
        with self._lock:
            self._state = mutator(self._state)
            return self._state

    def commit_review(self, lease: ReviewLease, reviewed: tuple[Any, ...], items: tuple[OutboxItem, ...],
                      reservations: tuple[tuple[str, str, str, int, datetime], ...],
                      silenced: tuple[SelfLoopCandidate, ...], at: datetime) -> bool:
        at = _utc(at)
        with self._lock:
            if not self.verify_review_lease(lease, at):
                return False
            for thought in reviewed:
                candidate = thought.candidate
                if self._candidates.get(candidate.identity) != candidate:
                    return False
            # Validate against local copies first: a rejected batch must not consume
            # any daily budget or leave a partially written outbox.
            reservations_copy = {key: value for key, value in self._reservations.items() if value[2] > at}
            for reservation_id, candidate_id, day, budget, expires_at in reservations:
                existing = reservations_copy.get(reservation_id)
                if existing is not None:
                    if existing != (candidate_id, day, expires_at):
                        raise IdempotencyConflict('reservation_id reused with different reservation')
                    continue
                count = sum(1 for _, value_day, _ in reservations_copy.values() if value_day == day)
                count += sum(1 for value_day in self._committed.values() if value_day == day)
                if count >= budget:
                    return False
                reservations_copy[reservation_id] = (candidate_id, day, expires_at)
            for item in items:
                previous = self._outbox.get(item.id)
                if previous is not None and previous != item:
                    raise IdempotencyConflict('outbox id reused with different payload')
            self._reservations = reservations_copy
            for item in items:
                self._outbox[item.id] = item
            for thought in reviewed:
                if not thought.retain_candidate:
                    del self._candidates[thought.candidate.identity]
            attention = dict(self._state.attention)
            silenced_state = dict(self._state.silenced)
            for candidate in silenced:
                silenced_state[candidate.topic] = at
                attention[candidate.topic] = (candidate.value, at)
            self._state = replace(self._state, attention=attention, silenced=silenced_state,
                                  last_review_at=at if reviewed else self._state.last_review_at)
            return True

    def _reserve_locked(self, reservation_id: str, candidate_id: str, day: str, budget: int,
                        now: datetime, expires_at: datetime) -> bool:
        self._reservations = {key: value for key, value in self._reservations.items() if value[2] > now}
        if reservation_id in self._reservations:
            return self._reservations[reservation_id] == (candidate_id, day, expires_at)
        count = sum(1 for _, value_day, _ in self._reservations.values() if value_day == day)
        count += sum(1 for value_day in self._committed.values() if value_day == day)
        if count >= budget:
            return False
        self._reservations[reservation_id] = candidate_id, day, expires_at
        return True

    def claim_outbox(self, owner: str, now: datetime, expires_at: datetime, *, limit: int = 1) -> tuple[OutboxItem, ...]:
        now, expires_at = _utc(now), _utc(expires_at)
        _claim_args(owner, now, expires_at, limit)
        with self._lock:
            claimed: list[OutboxItem] = []
            for key, item in list(self._outbox.items()):
                available = item.status == 'pending' or (item.status == 'claimed' and item.lease_expires_at is not None and item.lease_expires_at <= now)
                if available and len(claimed) < limit:
                    updated = replace(item, status='claimed', owner=owner, fencing=item.fencing + 1, lease_expires_at=expires_at, attempts=item.attempts + 1)
                    self._outbox[key] = updated
                    claimed.append(updated)
            return tuple(claimed)

    def claim_outbox_one(self, item_id: str, owner: str, now: datetime, expires_at: datetime) -> OutboxItem | None:
        if not isinstance(item_id, str) or not item_id:
            raise ValueError('item_id must be non-empty')
        now, expires_at = _utc(now), _utc(expires_at)
        _claim_args(owner, now, expires_at, 1)
        with self._lock:
            item = self._outbox.get(item_id)
            if item is None or not (item.status == 'pending' or (item.status == 'claimed' and item.lease_expires_at and item.lease_expires_at <= now)):
                return None
            claimed = replace(item, status='claimed', owner=owner, fencing=item.fencing + 1,
                              lease_expires_at=expires_at, attempts=item.attempts + 1)
            self._outbox[item_id] = claimed
            return claimed

    def ack_outbox(self, item: OutboxItem, now: datetime) -> bool:
        now = _utc(now)
        with self._lock:
            current = self._outbox.get(item.id)
            if current != item or item.status != 'claimed' or item.lease_expires_at is None:
                return False
            if item.lease_expires_at <= now:
                self._expire_outbox_locked(item)
                return False
            if item.kind == 'expression':
                data = json.loads(item.payload)
                if not self.commit_expression(data['reservation_id'], data['expression_id'], now):
                    return False
                expressions = self._state.expressions + ((data['topic'], now),)
                attention = dict(self._state.attention)
                attention[data['topic']] = (float(data['value']), now)
                self._state = replace(self._state, expressions=expressions, attention=attention)
            self._outbox[item.id] = replace(item, status='acked', lease_expires_at=None)
            return True

    def fail_outbox(self, item: OutboxItem, now: datetime) -> bool:
        now = _utc(now)
        with self._lock:
            if self._outbox.get(item.id) != item or item.status != 'claimed':
                return False
            if item.kind == 'expression':
                self._reservations.pop(json.loads(item.payload)['reservation_id'], None)
            self._outbox[item.id] = replace(item, status='failed', lease_expires_at=None)
            return True

    def release_outbox(self, item: OutboxItem, now: datetime) -> bool:
        now = _utc(now)
        with self._lock:
            current = self._outbox.get(item.id)
            if current != item or item.status != 'claimed':
                return False
            self._outbox[item.id] = replace(item, status='pending', owner=None, lease_expires_at=None)
            return True

    def _expire_outbox_locked(self, item: OutboxItem) -> None:
        if item.kind == 'expression':
            self._reservations.pop(json.loads(item.payload)['reservation_id'], None)
        self._outbox[item.id] = replace(item, status='expired', lease_expires_at=None)


class SQLiteStateStore:
    """Small SQLite store; each mutation uses an immediate transaction for process safety."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError('path must be a non-empty string')
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
            raise ValueError('busy_timeout_ms must be a non-negative integer')
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute(f'PRAGMA busy_timeout={busy_timeout_ms}')
        self._connection.executescript('''
            CREATE TABLE IF NOT EXISTS self_loop_candidates (identity TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS self_loop_review_fence (name TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL, fencing INTEGER NOT NULL, expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS self_loop_reservations (reservation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, day TEXT NOT NULL, expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS self_loop_committed (expression_id TEXT PRIMARY KEY, day TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS self_loop_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS self_loop_state (name TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS self_loop_outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')
        self._connection.execute("INSERT OR IGNORE INTO self_loop_meta VALUES ('generation', '0')")
        self._connection.execute("INSERT OR IGNORE INTO self_loop_meta VALUES ('sequence', '0')")
        self._connection.execute("INSERT OR IGNORE INTO self_loop_state VALUES ('state', '{}')")
        self._connection.commit()
        self._lock = RLock()

    def add_candidate(self, candidate: SelfLoopCandidate) -> SelfLoopCandidate:
        with self._tx() as conn:
            row = conn.execute('SELECT payload FROM self_loop_candidates WHERE identity = ?', (_identity(candidate),)).fetchone()
            if row is not None and _priority(candidate) <= _priority(_decode(row[0])):
                return _decode(row[0])
            generation = self._next(conn, 'generation')
            sequence = self._next(conn, 'sequence')
            stored = replace(candidate, candidate_id=uuid4().hex, generation=generation, sequence=sequence)
            conn.execute('INSERT OR REPLACE INTO self_loop_candidates VALUES (?, ?)', (_identity(stored), _encode(stored)))
            return stored

    def snapshot_candidates(self) -> tuple[SelfLoopCandidate, ...]:
        with self._lock:
            rows = self._connection.execute('SELECT payload FROM self_loop_candidates').fetchall()
        return tuple(sorted((_decode(row[0]) for row in rows), key=_priority, reverse=True))

    def ack_candidate(self, candidate: SelfLoopCandidate) -> bool:
        with self._tx() as conn:
            row = conn.execute('SELECT payload FROM self_loop_candidates WHERE identity = ?', (_identity(candidate),)).fetchone()
            if row is None or _decode(row[0]) != candidate:
                return False
            conn.execute('DELETE FROM self_loop_candidates WHERE identity = ?', (_identity(candidate),))
            return True

    def acquire_review_lease(self, owner: str, now: datetime, expires_at: datetime) -> bool:
        return self.acquire_review_lease_token(owner, now, expires_at) is not None

    def release_review_lease(self, owner: str) -> None:
        with self._tx() as conn:
            conn.execute('DELETE FROM self_loop_review_fence WHERE name = ? AND owner = ?', ('review', owner))

    def acquire_review_lease_token(self, owner: str, now: datetime, expires_at: datetime) -> ReviewLease | None:
        now, expires_at = _utc(now), _utc(expires_at)
        with self._tx() as conn:
            row = conn.execute('SELECT owner, fencing, expires_at FROM self_loop_review_fence WHERE name = ?', ('review',)).fetchone()
            if row is not None and _parse(row[2]) > now and row[0] != owner:
                return None
            fencing = (row[1] if row is not None else 0) + 1
            lease = ReviewLease(owner, f'{owner}:{fencing}', fencing, expires_at)
            conn.execute('INSERT OR REPLACE INTO self_loop_review_fence VALUES (?, ?, ?, ?, ?)', ('review', owner, lease.token, fencing, expires_at.isoformat()))
            return lease

    def verify_review_lease(self, lease: ReviewLease, now: datetime) -> bool:
        now = _utc(now)
        with self._lock:
            row = self._connection.execute('SELECT owner, token, fencing, expires_at FROM self_loop_review_fence WHERE name = ?', ('review',)).fetchone()
        return row is not None and row[:3] == (lease.owner, lease.token, lease.fencing) and _parse(row[3]) > now

    def renew_review_lease(self, lease: ReviewLease, now: datetime, expires_at: datetime) -> ReviewLease | None:
        now, expires_at = _utc(now), _utc(expires_at)
        with self._tx() as conn:
            row = conn.execute('SELECT owner, token, fencing, expires_at FROM self_loop_review_fence WHERE name = ?', ('review',)).fetchone()
            if row is None or row[:3] != (lease.owner, lease.token, lease.fencing) or _parse(row[3]) <= now:
                return None
            conn.execute('UPDATE self_loop_review_fence SET expires_at = ? WHERE name = ?', (expires_at.isoformat(), 'review'))
            return ReviewLease(lease.owner, lease.token, lease.fencing, expires_at)

    def release_review_lease_token(self, lease: ReviewLease) -> None:
        with self._tx() as conn:
            conn.execute('DELETE FROM self_loop_review_fence WHERE name = ? AND owner = ? AND token = ? AND fencing = ?', ('review', lease.owner, lease.token, lease.fencing))

    def reserve_expression(self, reservation_id: str, candidate_id: str, day: str, budget: int, now: datetime, expires_at: datetime) -> bool:
        now, expires_at = _utc(now), _utc(expires_at)
        if type(budget) is not int or budget < 0:
            raise ValueError('budget must be a non-negative integer')
        with self._tx() as conn:
            conn.execute('DELETE FROM self_loop_reservations WHERE expires_at <= ?', (now.isoformat(),))
            existing = conn.execute('SELECT candidate_id, day, expires_at FROM self_loop_reservations WHERE reservation_id = ?', (reservation_id,)).fetchone()
            if existing:
                if existing != (candidate_id, day, expires_at.isoformat()):
                    raise IdempotencyConflict('reservation_id reused with different reservation')
                return True
            count = conn.execute('SELECT COUNT(*) FROM self_loop_reservations WHERE day = ?', (day,)).fetchone()[0]
            count += conn.execute('SELECT COUNT(*) FROM self_loop_committed WHERE day = ?', (day,)).fetchone()[0]
            if count >= budget:
                return False
            conn.execute('INSERT INTO self_loop_reservations VALUES (?, ?, ?, ?)', (reservation_id, candidate_id, day, expires_at.isoformat()))
            return True

    def commit_expression(self, reservation_id: str, expression_id: str, at: datetime) -> bool:
        at = _utc(at)
        with self._tx() as conn:
            committed = conn.execute('SELECT day FROM self_loop_committed WHERE expression_id = ?', (expression_id,)).fetchone()
            if committed:
                reservation = conn.execute('SELECT day FROM self_loop_reservations WHERE reservation_id = ?', (reservation_id,)).fetchone()
                if reservation is not None and reservation[0] != committed[0]:
                    raise IdempotencyConflict('expression_id reused with different reservation')
                return False
            row = conn.execute('SELECT day, expires_at FROM self_loop_reservations WHERE reservation_id = ?', (reservation_id,)).fetchone()
            if row is None or _parse(row[1]) <= at:
                conn.execute('DELETE FROM self_loop_reservations WHERE reservation_id = ?', (reservation_id,))
                return False
            conn.execute('INSERT INTO self_loop_committed VALUES (?, ?)', (expression_id, row[0]))
            conn.execute('DELETE FROM self_loop_reservations WHERE reservation_id = ?', (reservation_id,))
            return True

    def release_expression(self, reservation_id: str, now: datetime) -> bool:
        _utc(now)
        with self._tx() as conn:
            return conn.execute('DELETE FROM self_loop_reservations WHERE reservation_id = ?', (reservation_id,)).rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def load_state(self) -> StoredState:
        with self._lock:
            row = self._connection.execute("SELECT payload FROM self_loop_state WHERE name = 'state'").fetchone()
        return _decode_state(json.loads(row[0]))

    def mutate_state(self, mutator: Any) -> StoredState:
        with self._tx() as conn:
            state = _decode_state(json.loads(conn.execute("SELECT payload FROM self_loop_state WHERE name = 'state'").fetchone()[0]))
            updated = mutator(state)
            if not isinstance(updated, StoredState):
                raise ValueError('state mutator must return StoredState')
            conn.execute("UPDATE self_loop_state SET payload = ? WHERE name = 'state'", (json.dumps(_encode_state(updated), sort_keys=True),))
            return updated

    def save_state(self, state: StoredState) -> None:
        self.mutate_state(lambda _: state)

    def commit_review(self, lease: ReviewLease, reviewed: tuple[Any, ...], items: tuple[OutboxItem, ...],
                      reservations: tuple[tuple[str, str, str, int, datetime], ...],
                      silenced: tuple[SelfLoopCandidate, ...], at: datetime) -> bool:
        at = _utc(at)
        with self._tx() as conn:
            row = conn.execute('SELECT owner, token, fencing, expires_at FROM self_loop_review_fence WHERE name = ?', ('review',)).fetchone()
            if row is None or row[:3] != (lease.owner, lease.token, lease.fencing) or _parse(row[3]) <= at:
                return False
            for thought in reviewed:
                current = conn.execute('SELECT payload FROM self_loop_candidates WHERE identity = ?', (_identity(thought.candidate),)).fetchone()
                if current is None or _decode(current[0]) != thought.candidate:
                    return False
            existing_rows = conn.execute('SELECT reservation_id, candidate_id, day, expires_at FROM self_loop_reservations WHERE expires_at > ?', (at.isoformat(),)).fetchall()
            simulated = {row[0]: (row[1], row[2], row[3]) for row in existing_rows}
            committed_counts = {row[0]: row[1] for row in conn.execute('SELECT day, COUNT(*) FROM self_loop_committed GROUP BY day').fetchall()}
            for reservation_id, candidate_id, day, budget, expires_at in reservations:
                existing = simulated.get(reservation_id)
                expected = (candidate_id, day, expires_at.isoformat())
                if existing is not None:
                    if existing != expected:
                        raise IdempotencyConflict('reservation_id reused with different reservation')
                    continue
                count = sum(1 for _, stored_day, _ in simulated.values() if stored_day == day) + committed_counts.get(day, 0)
                if count >= budget:
                    return False
                simulated[reservation_id] = expected
            conn.execute('DELETE FROM self_loop_reservations WHERE expires_at <= ?', (at.isoformat(),))
            existing_ids = {row[0] for row in existing_rows}
            for reservation_id, (candidate_id, day, expires_at) in simulated.items():
                if reservation_id not in existing_ids:
                    conn.execute('INSERT INTO self_loop_reservations VALUES (?, ?, ?, ?)', (reservation_id, candidate_id, day, expires_at))
            for item in items:
                prior = conn.execute('SELECT payload FROM self_loop_outbox WHERE id = ?', (item.id,)).fetchone()
                encoded = _encode_outbox(item)
                if prior is not None and prior[0] != encoded:
                    raise IdempotencyConflict('outbox id reused with different payload')
                conn.execute('INSERT OR IGNORE INTO self_loop_outbox VALUES (?, ?)', (item.id, encoded))
            for thought in reviewed:
                if not thought.retain_candidate:
                    conn.execute('DELETE FROM self_loop_candidates WHERE identity = ?', (_identity(thought.candidate),))
            state = _decode_state(json.loads(conn.execute("SELECT payload FROM self_loop_state WHERE name = 'state'").fetchone()[0]))
            attention, silenced_state = dict(state.attention), dict(state.silenced)
            for candidate in silenced:
                attention[candidate.topic], silenced_state[candidate.topic] = (candidate.value, at), at
            state = replace(state, attention=attention, silenced=silenced_state,
                            last_review_at=at if reviewed else state.last_review_at)
            conn.execute("UPDATE self_loop_state SET payload = ? WHERE name = 'state'", (json.dumps(_encode_state(state), sort_keys=True),))
            return True

    def claim_outbox(self, owner: str, now: datetime, expires_at: datetime, *, limit: int = 1) -> tuple[OutboxItem, ...]:
        now, expires_at = _utc(now), _utc(expires_at)
        _claim_args(owner, now, expires_at, limit)
        with self._tx() as conn:
            rows = conn.execute('SELECT id, payload FROM self_loop_outbox').fetchall()
            result: list[OutboxItem] = []
            for key, raw in rows:
                item = _decode_outbox(raw)
                available = item.status == 'pending' or (item.status == 'claimed' and item.lease_expires_at is not None and item.lease_expires_at <= now)
                if available and len(result) < limit:
                    item = replace(item, status='claimed', owner=owner, fencing=item.fencing + 1,
                                   lease_expires_at=expires_at, attempts=item.attempts + 1)
                    conn.execute('UPDATE self_loop_outbox SET payload = ? WHERE id = ?', (_encode_outbox(item), key))
                    result.append(item)
            return tuple(result)

    def claim_outbox_one(self, item_id: str, owner: str, now: datetime, expires_at: datetime) -> OutboxItem | None:
        if not isinstance(item_id, str) or not item_id:
            raise ValueError('item_id must be non-empty')
        now, expires_at = _utc(now), _utc(expires_at)
        _claim_args(owner, now, expires_at, 1)
        with self._tx() as conn:
            row = conn.execute('SELECT payload FROM self_loop_outbox WHERE id = ?', (item_id,)).fetchone()
            if row is None:
                return None
            item = _decode_outbox(row[0])
            if not (item.status == 'pending' or (item.status == 'claimed' and item.lease_expires_at and item.lease_expires_at <= now)):
                return None
            claimed = replace(item, status='claimed', owner=owner, fencing=item.fencing + 1,
                              lease_expires_at=expires_at, attempts=item.attempts + 1)
            conn.execute('UPDATE self_loop_outbox SET payload = ? WHERE id = ?', (_encode_outbox(claimed), item_id))
            return claimed

    def outbox(self) -> tuple[OutboxItem, ...]:
        with self._lock:
            return tuple(_decode_outbox(row[0]) for row in self._connection.execute('SELECT payload FROM self_loop_outbox').fetchall())

    def put_outbox(self, item: OutboxItem) -> OutboxItem:
        with self._tx() as conn:
            row = conn.execute('SELECT payload FROM self_loop_outbox WHERE id = ?', (item.id,)).fetchone()
            encoded = _encode_outbox(item)
            if row is not None and row[0] != encoded:
                raise IdempotencyConflict('outbox id reused with different payload')
            conn.execute('INSERT OR IGNORE INTO self_loop_outbox VALUES (?, ?)', (item.id, encoded))
            return item

    def ack_outbox(self, item: OutboxItem, now: datetime) -> bool:
        now = _utc(now)
        with self._tx() as conn:
            row = conn.execute('SELECT payload FROM self_loop_outbox WHERE id = ?', (item.id,)).fetchone()
            if row is None or _decode_outbox(row[0]) != item or item.status != 'claimed' or item.lease_expires_at is None:
                return False
            if item.lease_expires_at <= now:
                self._expire_outbox_tx(conn, item)
                return False
            if item.kind == 'expression' and not self._commit_expression_tx(conn, json.loads(item.payload), now):
                return False
            if item.kind == 'expression':
                state = _decode_state(json.loads(conn.execute("SELECT payload FROM self_loop_state WHERE name = 'state'").fetchone()[0]))
                data = json.loads(item.payload)
                state = replace(state, expressions=state.expressions + ((data['topic'], now),),
                                attention={**state.attention, data['topic']: (float(data['value']), now)})
                conn.execute("UPDATE self_loop_state SET payload = ? WHERE name = 'state'", (json.dumps(_encode_state(state), sort_keys=True),))
            conn.execute('UPDATE self_loop_outbox SET payload = ? WHERE id = ?', (_encode_outbox(replace(item, status='acked', lease_expires_at=None)), item.id))
            return True

    def release_outbox(self, item: OutboxItem, now: datetime) -> bool:
        now = _utc(now)
        with self._tx() as conn:
            row = conn.execute('SELECT payload FROM self_loop_outbox WHERE id = ?', (item.id,)).fetchone()
            if row is None or _decode_outbox(row[0]) != item or item.status != 'claimed':
                return False
            conn.execute('UPDATE self_loop_outbox SET payload = ? WHERE id = ?', (_encode_outbox(replace(item, status='pending', owner=None, lease_expires_at=None)), item.id))
            return True

    def fail_outbox(self, item: OutboxItem, now: datetime) -> bool:
        now = _utc(now)
        with self._tx() as conn:
            row = conn.execute('SELECT payload FROM self_loop_outbox WHERE id = ?', (item.id,)).fetchone()
            if row is None or _decode_outbox(row[0]) != item or item.status != 'claimed':
                return False
            if item.kind == 'expression':
                conn.execute('DELETE FROM self_loop_reservations WHERE reservation_id = ?', (json.loads(item.payload)['reservation_id'],))
            conn.execute('UPDATE self_loop_outbox SET payload = ? WHERE id = ?', (_encode_outbox(replace(item, status='failed', lease_expires_at=None)), item.id))
            return True

    @staticmethod
    def _expire_outbox_tx(conn: sqlite3.Connection, item: OutboxItem) -> None:
        if item.kind == 'expression':
            conn.execute('DELETE FROM self_loop_reservations WHERE reservation_id = ?', (json.loads(item.payload)['reservation_id'],))
        conn.execute('UPDATE self_loop_outbox SET payload = ? WHERE id = ?', (_encode_outbox(replace(item, status='expired', lease_expires_at=None)), item.id))

    def _commit_expression_tx(self, conn: sqlite3.Connection, data: dict[str, Any], at: datetime) -> bool:
        if conn.execute('SELECT 1 FROM self_loop_committed WHERE expression_id = ?', (data['expression_id'],)).fetchone():
            return False
        row = conn.execute('SELECT day, expires_at FROM self_loop_reservations WHERE reservation_id = ?', (data['reservation_id'],)).fetchone()
        if row is None or _parse(row[1]) <= at:
            return False
        conn.execute('INSERT INTO self_loop_committed VALUES (?, ?)', (data['expression_id'], row[0]))
        conn.execute('DELETE FROM self_loop_reservations WHERE reservation_id = ?', (data['reservation_id'],))
        return True

    @staticmethod
    def _next(conn: sqlite3.Connection, name: str) -> int:
        value = int(conn.execute('SELECT value FROM self_loop_meta WHERE name = ?', (name,)).fetchone()[0]) + 1
        conn.execute('UPDATE self_loop_meta SET value = ? WHERE name = ?', (str(value), name))
        return value

    def _tx(self):
        class Transaction:
            def __init__(self, store: SQLiteStateStore) -> None:
                self.store = store
            def __enter__(self):
                self.store._lock.acquire()
                try:
                    self.store._connection.execute('BEGIN IMMEDIATE')
                    return self.store._connection
                except BaseException:
                    self.store._lock.release()
                    raise
            def __exit__(self, exc_type, exc, traceback):
                try:
                    if exc_type is not None:
                        self.store._connection.rollback()
                    else:
                        self.store._connection.commit()
                except BaseException:
                    try:
                        self.store._connection.rollback()
                    finally:
                        raise
                finally:
                    self.store._lock.release()
        return Transaction(self)


def _encode(candidate: SelfLoopCandidate) -> str:
    value = asdict(candidate)
    value['observed_at'] = candidate.observed_at.isoformat()
    return json.dumps(value, sort_keys=True)


def _decode(value: str) -> SelfLoopCandidate:
    data = json.loads(value)
    data['observed_at'] = _parse(data['observed_at'])
    data['related_memory_ids'] = tuple(data['related_memory_ids'])
    candidate = SelfLoopCandidate(**data)
    if candidate.generation <= 0 or candidate.sequence <= 0 or not candidate.candidate_id:
        raise ValueError('persisted candidate must have assigned generation, sequence, and id')
    return candidate


def _parse(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _identity(candidate: SelfLoopCandidate) -> str:
    return '\x1f'.join(candidate.identity)


def _priority(candidate: SelfLoopCandidate) -> tuple[float, float, float, float]:
    return candidate.urgency, candidate.value, candidate.freshness, -candidate.interruption_cost


def _claim_args(owner: str, now: datetime, expires_at: datetime, limit: int) -> None:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError('owner must be a non-empty string')
    if type(limit) is not int or limit <= 0:
        raise ValueError('limit must be a positive integer')
    if expires_at <= now:
        raise ValueError('outbox lease must expire after now')


def _encode_state(state: StoredState) -> dict[str, Any]:
    def dates(values: dict[str, datetime]) -> dict[str, str]:
        return {key: value.isoformat() for key, value in values.items()}
    return {
        'attention': {key: [value, at.isoformat()] for key, (value, at) in state.attention.items()},
        'unresolved_ideas': dates(state.unresolved_ideas), 'silenced': dates(state.silenced),
        'waiting_conditions': dates(state.waiting_conditions),
        'expressions': [[topic, at.isoformat()] for topic, at in state.expressions],
        'last_review_at': state.last_review_at.isoformat() if state.last_review_at else None,
    }


def _decode_state(data: dict[str, Any]) -> StoredState:
    return StoredState(
        {key: (float(value[0]), _parse(value[1])) for key, value in data.get('attention', {}).items()},
        {key: _parse(value) for key, value in data.get('unresolved_ideas', {}).items()},
        {key: _parse(value) for key, value in data.get('silenced', {}).items()},
        {key: _parse(value) for key, value in data.get('waiting_conditions', {}).items()},
        tuple((value[0], _parse(value[1])) for value in data.get('expressions', ())),
        _parse(data['last_review_at']) if data.get('last_review_at') else None,
    )


def _encode_outbox(item: OutboxItem) -> str:
    data = asdict(item)
    data['created_at'] = item.created_at.isoformat()
    data['lease_expires_at'] = item.lease_expires_at.isoformat() if item.lease_expires_at else None
    return json.dumps(data, sort_keys=True)


def _decode_outbox(value: str) -> OutboxItem:
    data = json.loads(value)
    data['created_at'] = _parse(data['created_at'])
    data['lease_expires_at'] = _parse(data['lease_expires_at']) if data['lease_expires_at'] else None
    return OutboxItem(**data)
