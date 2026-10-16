from __future__ import annotations

import sys
from threading import Thread
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.self_loop.collector import CandidateCollector
from guanjia.self_loop.contracts import SelfLoopCandidate
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
