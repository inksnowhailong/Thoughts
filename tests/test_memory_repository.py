from __future__ import annotations

import sys
import sqlite3
from datetime import datetime
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
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


def test_injected_connection_respects_outer_rollback() -> None:
    connection = sqlite3.connect(':memory:')
    MemoryRepository.sqlite(connection)
    connection.execute('CREATE TABLE caller_data (value TEXT)')
    connection.commit()
    connection.execute('BEGIN')
    connection.execute('INSERT INTO caller_data VALUES (?)', ('outer',))
    repository = MemoryRepository.sqlite(connection)
    repository.write(MemoryCandidate('owner.city', 'Shanghai', 'fact', MemorySource.USER, 0.9, 8))
    connection.rollback()
    assert connection.execute('SELECT value FROM caller_data').fetchall() == []
    assert connection.execute('SELECT payload FROM guanjia_memories').fetchall() == []


def test_injected_initialization_does_not_commit_caller_work() -> None:
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE caller_data (value TEXT)')
    connection.commit()
    connection.execute('BEGIN')
    connection.execute('INSERT INTO caller_data VALUES (?)', ('outer',))
    MemoryRepository.sqlite(connection)
    connection.rollback()
    assert connection.execute('SELECT value FROM caller_data').fetchall() == []


def test_sqlite_repositories_serialize_same_key_updates_and_keep_conflict() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'memory.db'
        first = MemoryRepository.sqlite_path(path)
        second = MemoryRepository.sqlite_path(path)
        first.write(MemoryCandidate('owner.city', 'Beijing', 'fact', MemorySource.USER, 0.9, 8))
        gate = Barrier(2)

        def write(repository: MemoryRepository, content: str) -> None:
            gate.wait()
            repository.write(MemoryCandidate('owner.city', content, 'fact', MemorySource.OBSERVATION, 0.91, 8))

        left = Thread(target=write, args=(first, 'Guangzhou'))
        right = Thread(target=write, args=(second, 'Shenzhen'))
        left.start()
        right.start()
        left.join()
        right.join()

        record = first.get('owner.city')
        assert record is not None and record.content == 'Beijing'
        assert {conflict.incoming.content for conflict in first.conflicts()} == {'Guangzhou', 'Shenzhen'}
        first.close()
        second.close()


def test_old_conflict_cannot_overwrite_later_fact_and_is_removed() -> None:
    for repository in (MemoryRepository.in_memory(),):
        repository.write(MemoryCandidate('owner.city', 'Beijing', 'fact', MemorySource.USER, 0.9, 8))
        repository.write(MemoryCandidate('owner.city', 'Guangzhou', 'fact', MemorySource.OBSERVATION, 0.91, 8))
        conflict = repository.conflicts()[0]
        repository.write(MemoryCandidate('owner.city', 'Shanghai', 'fact', MemorySource.USER_CORRECTION, 0.1, 8))

        try:
            repository.resolve_conflict(conflict.id, use_incoming=True)
        except (KeyError, ValueError) as error:
            assert not isinstance(error, ValueError) or str(error) == 'conflict is stale'
        else:
            raise AssertionError('stale conflict resolution must be rejected')
        assert repository.get('owner.city').content == 'Shanghai'
        assert not repository.conflicts()


def test_persisted_invalid_datetime_is_value_error() -> None:
    repository = MemoryRepository.sqlite(sqlite3.connect(':memory:'))
    payload = (
        '{"id":"id","key":"owner.city","content":"Beijing","memory_type":"fact",'
        '"source":"user","confidence":0.9,"importance":8,"retention":"adaptive",'
        '"people":[],"evidence":[],"media":[],"conflict_key":null,'
        '"created_at":"2026-01-01T00:00:00","updated_at":"not-a-date",'
        '"last_used_at":null,"use_count":0}'
    )
    repository._backend._connection.execute(  # type: ignore[attr-defined]
        'INSERT INTO guanjia_memories(key, payload) VALUES(?, ?)', ('owner.city', payload)
    )
    try:
        repository.get('owner.city')
    except ValueError:
        pass
    else:
        raise AssertionError('invalid persisted datetime must raise ValueError')


def test_conflict_choice_and_mark_used_timestamp_are_strict() -> None:
    repository = MemoryRepository.in_memory()
    record = repository.write(MemoryCandidate('owner.city', 'Beijing', 'fact', MemorySource.USER, 0.9, 8))
    try:
        repository.resolve_conflict('missing', 1)  # type: ignore[arg-type]
    except ValueError as error:
        assert str(error) == 'use_incoming must be a bool'
    else:
        raise AssertionError('non-bool conflict choice must be rejected')
    try:
        repository.mark_used(record.id, datetime(2026, 10, 16))
    except ValueError as error:
        assert str(error) == 'at must be an aware UTC datetime'
    else:
        raise AssertionError('naive usage timestamp must be rejected')
