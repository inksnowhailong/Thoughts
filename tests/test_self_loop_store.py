from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.self_loop.contracts import SelfLoopCandidate
from guanjia.self_loop.state import SelfLoopState
from guanjia.self_loop.store import InMemoryStateStore, OutboxItem, SQLiteStateStore


def _candidate(now: datetime, topic: str = 'weather') -> SelfLoopCandidate:
    return SelfLoopCandidate(topic, 'rain', 0.8, 0.2, 0.8, 0.1, now)


def test_sqlite_allocates_monotonic_candidates_and_fences_expired_reviews() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'loop.db'
        first, second = SQLiteStateStore(path), SQLiteStateStore(path)
        one, two = first.add_candidate(_candidate(now)), second.add_candidate(_candidate(now, 'calendar'))
        assert (two.generation, two.sequence) > (one.generation, one.sequence)
        old = first.acquire_review_lease_token('one', now, now + timedelta(seconds=1))
        assert old is not None
        renewed = first.renew_review_lease(old, now, now + timedelta(seconds=2))
        assert renewed is not None
        current = second.acquire_review_lease_token('two', now + timedelta(seconds=3), now + timedelta(seconds=4))
        assert current is not None and current.fencing > old.fencing
        assert not first.verify_review_lease(renewed, now + timedelta(seconds=3))
        first.close()
        second.close()


def test_sqlite_outbox_claim_is_fenced_and_survives_restart() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'loop.db'
        store = SQLiteStateStore(path)
        item = OutboxItem('event-1', 'internal', 'candidate-1', '{}', 'pending', now)
        store.put_outbox(item)
        claimed = store.claim_outbox('one', now, now + timedelta(seconds=1))[0]
        store.close()
        restarted = SQLiteStateStore(path)
        assert not restarted.ack_outbox(claimed, now + timedelta(seconds=2))
        assert restarted.outbox()[0].status == 'expired'
        assert not restarted.claim_outbox('two', now + timedelta(seconds=2), now + timedelta(seconds=3))
        restarted.close()


def test_store_state_mutation_preserves_updates_from_two_facades() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    store = InMemoryStateStore()
    first, second = SelfLoopState(store=store), SelfLoopState(store=store)
    first.add_unresolved('first idea', now)
    second.add_unresolved('second idea', now)
    assert set(store.load_state().unresolved_ideas) == {'first idea', 'second idea'}


def test_sqlite_rejected_review_budget_has_no_partial_writes() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    with TemporaryDirectory() as directory:
        store = SQLiteStateStore(Path(directory) / 'loop.db')
        candidate = store.add_candidate(_candidate(now))
        lease = store.acquire_review_lease_token('reviewer', now, now + timedelta(minutes=1))
        assert lease is not None
        thought = SimpleNamespace(candidate=candidate, retain_candidate=False)
        items = (
            OutboxItem('one', 'expression', candidate.candidate_id, '{}', 'pending', now),
            OutboxItem('two', 'expression', candidate.candidate_id, '{}', 'pending', now),
        )
        reservations = (
            ('reserve-one', candidate.candidate_id, now.date().isoformat(), 1, now + timedelta(minutes=1)),
            ('reserve-two', candidate.candidate_id, now.date().isoformat(), 1, now + timedelta(minutes=1)),
        )
        assert not store.commit_review(lease, (thought,), items, reservations, (), now)
        assert store.snapshot_candidates() == (candidate,)
        assert not store.outbox()
        assert not store._connection.execute('SELECT * FROM self_loop_reservations').fetchall()
        assert store.load_state().last_review_at is None
        store.close()
