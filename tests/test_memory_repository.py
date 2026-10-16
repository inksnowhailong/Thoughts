from __future__ import annotations

import sys
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.memory.contracts import MediaRef, MemoryCandidate, MemorySource, RetentionLevel
from guanjia.memory.repository import MemoryRepository


def test_repository_writes_queries_tracks_use_and_media() -> None:
    repository = MemoryRepository.in_memory()
    candidate = MemoryCandidate(
        'owner.preference.coffee',
        'prefers black coffee',
        'preference',
        MemorySource.USER,
        0.9,
        7,
        RetentionLevel.LONG_TERM,
        people=('owner',),
        media=(MediaRef('media-1', 'image', 'sha256:x'),),
    )
    record = repository.write(candidate)
    assert repository.query_text('black coffee').records == (record,)
    used = repository.mark_used(record.id)
    assert used.use_count == 1
    assert used.media[0].media_id == 'media-1'


def test_repository_accepts_a_temporary_sqlite_connection() -> None:
    repository = MemoryRepository.sqlite(sqlite3.connect(':memory:'))
    written = repository.write(
        MemoryCandidate('owner.city', 'Shanghai', 'fact', MemorySource.USER, 0.9, 8)
    )
    assert repository.get('owner.city') == written


def test_repository_keeps_current_value_when_evidence_is_not_decisive() -> None:
    repository = MemoryRepository.in_memory()
    repository.write(MemoryCandidate('owner.city', 'Beijing', 'fact', MemorySource.USER, 0.9, 8))
    result = repository.write(MemoryCandidate('owner.city', 'Guangzhou', 'fact', MemorySource.OBSERVATION, 0.91, 8))
    assert result.content == 'Beijing'
    assert repository.conflicts()[0].status == 'uncertain'
    repository.write(MemoryCandidate('owner.city', 'Guangzhou', 'fact', MemorySource.OBSERVATION, 0.91, 8))
    assert len(repository.conflicts()) == 1


def test_conflict_survives_sqlite_repository_restart() -> None:
    connection = sqlite3.connect(':memory:')
    repository = MemoryRepository.sqlite(connection)
    repository.write(MemoryCandidate('owner.city', 'Beijing', 'fact', MemorySource.USER, 0.9, 8))
    repository.write(MemoryCandidate('owner.city', 'Guangzhou', 'fact', MemorySource.OBSERVATION, 0.91, 8))
    restarted = MemoryRepository.sqlite(connection)
    conflict = restarted.conflicts()[0]
    assert restarted.resolve_conflict(conflict.id, use_incoming=True).content == 'Guangzhou'
    assert not restarted.conflicts()
    assert restarted.get('owner.city').content == 'Guangzhou'


def test_sqlite_path_allows_shared_thread_reads_and_writes() -> None:
    with TemporaryDirectory() as directory:
        repository = MemoryRepository.sqlite_path(Path(directory) / 'memory.db')

        def write(index: int) -> None:
            repository.write(MemoryCandidate(f'owner.fact.{index}', str(index), 'fact', MemorySource.USER, 0.9, 1))

        threads = [Thread(target=write, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(repository.query_text('', limit=100).records) == 8
        repository.close()
