from __future__ import annotations

import sys
from threading import Thread
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.self_loop.collector import CandidateCollector
from guanjia.self_loop.contracts import CompressedSelfLoopState, SelfLoopCandidate
from guanjia.self_loop.state import SelfLoopState


def test_collector_deduplicates_and_state_compresses_old_details() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    collector = CandidateCollector()
    collector.add(SelfLoopCandidate('home', 'door opened', 0.6, 0.1, 0.9, 0.2, now))
    collector.add(SelfLoopCandidate('home', 'door opened', 0.8, 0.2, 0.8, 0.2, now))
    assert len(collector.drain()) == 1
    assert len(collector) == 1
    candidate = collector.snapshot()[0]
    assert collector.ack(candidate) and not len(collector)

    state = SelfLoopState()
    state.record_silence(SelfLoopCandidate('home', 'door opened', 0.6, 0.1, 0.9, 0.2, now), now)
    compact = state.compress(now + timedelta(minutes=31))
    assert compact.silenced_topics == ('home',)


def test_state_prunes_expired_entries() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    state = SelfLoopState(ttl=timedelta(minutes=30), expression_limit=2)
    candidate = SelfLoopCandidate('home', 'door opened', 0.6, 0.1, 0.9, 0.2, now)
    state.record_silence(candidate, now)
    state.record_expression(candidate, now)
    state.prune(now + timedelta(minutes=31))
    assert not state.silenced and not state.expressions


def test_collector_retains_a_consistent_snapshot_under_concurrent_adds() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    collector = CandidateCollector()

    def add_candidate(index: int) -> None:
        collector.add(SelfLoopCandidate(f'topic-{index}', 'update', 0.5, 0.1, 0.5, 0.1, now))

    threads = [Thread(target=add_candidate, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(collector.snapshot()) == 20


def test_collector_ack_is_compare_and_swap_safe() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    collector = CandidateCollector()
    first = SelfLoopCandidate('home', 'door opened', 0.5, 0.1, 0.5, 0.1, now)
    newer = SelfLoopCandidate('home', 'door opened', 0.9, 0.1, 0.5, 0.1, now)
    collector.add(first)
    stale_snapshot = collector.snapshot()[0]
    collector.add(newer)
    assert not collector.ack(stale_snapshot)
    assert collector.snapshot()[0].identity == newer.identity
    assert collector.snapshot()[0].generation > stale_snapshot.generation


def test_compressed_state_rejects_non_tuple_or_invalid_elements() -> None:
    try:
        CompressedSelfLoopState(('home',), [], (), (), ())
    except ValueError:
        return
    raise AssertionError('list accepted for compressed state field')
