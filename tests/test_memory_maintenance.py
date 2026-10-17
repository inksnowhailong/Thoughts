from datetime import datetime, timedelta, timezone

from dataclasses import replace
import sqlite3

from guanjia.memory.contracts import EvidenceRef, MediaRef, MemoryRecord, MemorySource, RetentionLevel
from guanjia.memory.maintenance import MaintenanceBudget, MaintenancePlanner
from guanjia.memory.media import (
    InMemoryMediaStateStore,
    MemoryMediaRetention,
    MemoryMediaService,
    SQLiteFencedMediaPortAdapter,
    SQLiteMemoryMediaStateStore,
)


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def record(
    record_id: str,
    content: str,
    *,
    retention: RetentionLevel = RetentionLevel.ADAPTIVE,
    importance: int = 4,
    use_count: int = 0,
    last_used_at: datetime | None = None,
    evidence: tuple[EvidenceRef, ...] = (),
) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        key=record_id,
        content=content,
        memory_type='fact',
        source=MemorySource.USER,
        confidence=0.9,
        importance=importance,
        retention=retention,
        evidence=evidence,
        created_at=NOW - timedelta(days=100),
        updated_at=NOW - timedelta(days=100),
        last_used_at=last_used_at,
        use_count=use_count,
    )


def test_planner_generates_immutable_merge_retention_expiry_and_reindex_actions() -> None:
    planner = MaintenancePlanner(
        MaintenanceBudget(max_records=3, temporary_ttl=timedelta(days=30), index_stale_after=timedelta(days=7))
    )
    evidence = EvidenceRef('session:1', MemorySource.USER, NOW)
    plan = planner.plan(
        (
            record('duplicate-low', 'Prefers coffee', importance=3),
            record('duplicate-high', ' prefers  coffee ', importance=8, use_count=2),
            record('promote', 'important preference', importance=8, evidence=(evidence,)),
            record('expired', 'old temporary', retention=RetentionLevel.TEMPORARY),
        ),
        now=NOW,
        index_built_at=NOW - timedelta(days=8),
    )

    assert [(action.kind, action.record_ids) for action in plan.actions] == [
        ('merge', ('duplicate-high', 'duplicate-low')),
        ('adjust_retention', ('duplicate-high',)),
        ('adjust_retention', ('promote',)),
        ('expire', ('expired',)),
        ('rebuild_index', ()),
    ]
    assert plan.actions[0].target_record_id == 'duplicate-high'
    assert plan.actions[2].retention is RetentionLevel.LONG_TERM
    assert next(action for action in plan.actions if action.kind == 'expire').reason == 'temporary retention expired without evidence'
    assert plan.budget_before == 4
    assert plan.budget_after == 2


def test_planner_protects_evidenced_and_recent_records_when_trimming_budget() -> None:
    planner = MaintenancePlanner(MaintenanceBudget(max_records=1))
    evidence = EvidenceRef('session:2', MemorySource.USER, NOW)
    plan = planner.plan(
        (
            record('protected', 'has evidence', evidence=(evidence,)),
            record('recent', 'recently used', last_used_at=NOW - timedelta(days=1)),
        ),
        now=NOW,
    )

    assert not [action for action in plan.actions if action.kind == 'expire']
    assert plan.budget_before == plan.budget_after == 2
    assert plan.rejections == ('budget exceeded; no safe expiry candidate',)


def test_planner_does_not_merge_equal_content_across_memory_types_or_people() -> None:
    planner = MaintenancePlanner(MaintenanceBudget(max_records=10))
    preference = replace(record('preference', 'same fact'), people=('owner',))
    task = replace(record('task', ' same fact '), memory_type='task', people=('owner',))
    family = replace(record('family', 'same fact'), people=('family',))

    plan = planner.plan((preference, task, family), now=NOW)

    assert not [action for action in plan.actions if action.kind == 'merge']


def test_planner_rejects_non_utc_clock_values() -> None:
    planner = MaintenancePlanner(MaintenanceBudget(max_records=1))

    try:
        planner.plan((), now=datetime(2026, 10, 16))
    except ValueError as error:
        assert str(error) == 'now must be an aware UTC datetime'
    else:
        raise AssertionError('non-UTC clocks must be rejected')


def test_media_service_persists_reasons_and_keeps_duplicate_retain_idempotent() -> None:
    class MediaPort:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool:
            self.calls.append(('retain' if retained else 'release', media_id, stable_ref))
            return True

        def get_metadata(self, media_id: str) -> dict[str, str]:
            return {'media_id': media_id}

    port = MediaPort()
    store = InMemoryMediaStateStore()
    service = MemoryMediaService(port, store)
    original = record('with-media', 'image evidence')
    updated = service.retain(original, 'photo-1', 'image', 'sha256:1', 'task evidence')
    duplicate = service.retain(updated.record, 'photo-1', 'image', 'sha256:1', 'task evidence')

    assert original.media == ()
    assert updated.record.media[0].media_id == 'photo-1'
    assert updated.reasons == {'photo-1': ('task evidence',)}
    assert duplicate.record == updated.record
    assert store.get('with-media', 'photo-1').retention.reasons == ('task evidence',)
    assert store.get('with-media', 'photo-1').record == updated.record
    service.reconcile()
    assert port.calls == [('retain', 'photo-1', 'memory:with-media:photo-1')]


def test_media_service_reloads_retention_and_releases_only_last_reason() -> None:
    class MediaPort:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool:
            self.calls.append(('retain' if retained else 'release', media_id, stable_ref))
            return True

        def get_metadata(self, media_id: str) -> None:
            return None

    port = MediaPort()
    store = InMemoryMediaStateStore()
    original = record('with-media', 'image evidence')
    retained = MemoryMediaService(port, store).retain(original, 'photo-1', 'image', 'sha256:1', 'task evidence').record
    retained = MemoryMediaService(port, store).retain(retained, 'photo-1', 'image', 'sha256:1', 'long-term memory').record
    restarted = MemoryMediaService(port, store)

    still_retained = restarted.release(original, 'photo-1', 'task evidence')
    missing = restarted.release(still_retained.record, 'photo-1', 'unknown reason')
    released = restarted.release(missing.record, 'photo-1', 'long-term memory')

    assert still_retained.record.media[0].media_id == 'photo-1'
    assert still_retained.record == retained
    assert missing.record == still_retained.record
    assert released.record.media == ()
    assert store.get('with-media', 'photo-1') is None
    restarted.reconcile()
    assert port.calls == [
        ('release', 'photo-1', 'memory:with-media:photo-1'),
    ]


def test_media_service_keeps_pending_retain_when_port_retain_fails() -> None:
    class FailingMediaPort:
        def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool:
            raise RuntimeError('media unavailable')

        def get_metadata(self, media_id: str) -> None:
            return None

    store = InMemoryMediaStateStore()
    service = MemoryMediaService(FailingMediaPort(), store)
    original = record('with-media', 'image evidence')

    service.retain(original, 'photo-1', 'image', 'sha256:1', 'task evidence')
    try:
        service.reconcile()
    except RuntimeError as error:
        assert str(error) == 'media unavailable'
    else:
        raise AssertionError('port failure must be raised')
    assert store.get('with-media', 'photo-1').retention.reasons == ('task evidence',)
    assert original.media == ()


def test_media_service_keeps_pending_release_when_port_release_fails() -> None:
    class FailingReleasePort:
        def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool:
            raise RuntimeError('media unavailable')

        def get_metadata(self, media_id: str) -> None:
            return None

    store = InMemoryMediaStateStore()
    existing = replace(
        record('with-media', 'image evidence'),
        media=(MediaRef('photo-1', 'image', 'sha256:1'),),
    )
    store.save(existing, MemoryMediaRetention('with-media', 'photo-1', ('task evidence',)))

    service = MemoryMediaService(FailingReleasePort(), store)
    service.release(existing, 'photo-1', 'task evidence')
    try:
        service.reconcile()
    except RuntimeError as error:
        assert str(error) == 'media unavailable'
    else:
        raise AssertionError('port failure must be raised')
    assert store.get('with-media', 'photo-1') is None
    assert existing.media[0].media_id == 'photo-1'


def test_media_service_rejects_conflicting_reference_without_side_effects() -> None:
    class MediaPort:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool:
            self.calls.append((media_id, stable_ref))
            return True

        def get_metadata(self, media_id: str) -> None:
            return None

    port = MediaPort()
    store = InMemoryMediaStateStore()
    existing = replace(record('with-media', 'image evidence'), media=(MediaRef('photo-1', 'image', 'sha256:1'),))

    try:
        MemoryMediaService(port, store).retain(existing, 'photo-1', 'video', 'sha256:2', 'task evidence')
    except ValueError as error:
        assert str(error) == 'media reference conflicts with existing record media'
    else:
        raise AssertionError('conflicting media identity must be rejected')
    assert port.calls == []
    assert store.get('with-media', 'photo-1') is None


def test_media_services_merge_concurrent_reasons_and_reconcile_after_restart(tmp_path) -> None:
    class IdempotentPort:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def apply_reference(self, media_id: str, stable_ref: str, retained: bool, generation: int) -> bool:
            self.calls.append(('retain' if retained else 'release', media_id, stable_ref))
            return True

        def get_metadata(self, media_id: str) -> None:
            return None

    port = IdempotentPort()
    store = SQLiteMemoryMediaStateStore(tmp_path / 'media.db')
    original = record('with-media', 'image evidence')
    first = MemoryMediaService(port, store)
    second_store = SQLiteMemoryMediaStateStore(tmp_path / 'media.db')
    second = MemoryMediaService(port, second_store)
    first.retain(original, 'photo-1', 'image', 'sha256:1', 'task evidence')
    second.retain(original, 'photo-1', 'image', 'sha256:1', 'long-term memory')
    assert port.calls == []

    restarted_store = SQLiteMemoryMediaStateStore(tmp_path / 'media.db')
    restarted = MemoryMediaService(port, restarted_store)
    restarted.reconcile()
    restarted.reconcile()
    state = store.get('with-media', 'photo-1')
    assert state is not None
    assert set(state.retention.reasons) == {'task evidence', 'long-term memory'}
    assert port.calls == [('retain', 'photo-1', 'memory:with-media:photo-1')]
    restarted_store.close()
    second_store.close()
    store.close()


def test_media_state_keeps_all_media_after_interleaved_updates() -> None:
    store = InMemoryMediaStateStore()
    service = MemoryMediaService(type('Port', (), {
        'retain': lambda *_: None, 'release': lambda *_: None, 'get_metadata': lambda *_: None,
    })(), store)
    original = record('with-media', 'image evidence')
    first = service.retain(original, 'one', 'image', 'sha256:one', 'one reason').record
    second = service.retain(first, 'two', 'image', 'sha256:two', 'two reason').record
    released = service.release(second, 'one', 'one reason').record
    assert {item.media_id for item in released.media} == {'two'}
    assert store.get('with-media', 'two').record == released


def test_sqlite_media_state_normalizes_record_across_media(tmp_path) -> None:
    class Port:
        def retain(self, *_: str) -> None: pass
        def release(self, *_: str) -> None: pass
        def get_metadata(self, *_: str) -> None: return None

    store = SQLiteMemoryMediaStateStore(tmp_path / 'nested' / 'media.db')
    service = MemoryMediaService(Port(), store)
    original = record('with-media', 'image evidence')
    first = service.retain(original, 'one', 'image', 'sha256:one', 'one reason').record
    second = service.retain(first, 'two', 'image', 'sha256:two', 'two reason').record
    released = service.release(second, 'one', 'one reason').record
    assert {item.media_id for item in released.media} == {'two'}
    assert store.get('with-media', 'two').record == released
    store.close()


def test_media_outbox_claim_fences_old_worker_and_allows_expired_takeover(tmp_path) -> None:
    store = SQLiteMemoryMediaStateStore(tmp_path / 'media.db')
    service = MemoryMediaService(type('Port', (), {
        'retain': lambda *_: None, 'release': lambda *_: None, 'get_metadata': lambda *_: None,
    })(), store)
    service.retain(record('with-media', 'image evidence'), 'one', 'image', 'sha256:one', 'reason')
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    first = store.claim(limit=1, owner='one', now=now)
    assert len(first) == 1
    assert not store.claim(limit=1, owner='two', now=now)
    second = store.claim(limit=1, owner='two', now=now + timedelta(minutes=6))
    assert len(second) == 1 and second[0].fencing > first[0].fencing
    assert not store.complete_operation(first[0], owner='one')
    assert store.complete_operation(second[0], owner='two')
    store.close()


def test_fenced_media_port_rejects_old_retain_after_new_release(tmp_path) -> None:
    first = SQLiteFencedMediaPortAdapter(tmp_path / 'port.db')
    second = SQLiteFencedMediaPortAdapter(tmp_path / 'port.db')
    assert first.apply_reference('photo', 'memory:record:photo', True, 1)
    assert second.apply_reference('photo', 'memory:record:photo', False, 2)
    assert not first.apply_reference('photo', 'memory:record:photo', True, 1)
    row = second._connection.execute(
        'SELECT retained, generation FROM guanjia_fenced_media_references WHERE stable_ref = ?',
        ('memory:record:photo',),
    ).fetchone()
    assert row == (0, 2)
    first.close()
    second.close()


def test_legacy_media_migration_uses_highest_revision_once_and_never_replays(tmp_path) -> None:
    path = tmp_path / 'legacy.db'
    old = sqlite3.connect(path)
    old.execute(
        'CREATE TABLE guanjia_memory_media_states '
        '(record_key TEXT, media_id TEXT, record_payload TEXT, reasons_payload TEXT, revision INTEGER)'
    )
    base = record('legacy', 'old')
    latest = replace(base, content='new')
    from guanjia.memory.repository import _encode
    old.executemany(
        'INSERT INTO guanjia_memory_media_states VALUES(?, ?, ?, ?, ?)',
        [
            ('legacy', 'one', _encode(base), '["one"]', 1),
            ('legacy', 'two', _encode(latest), '["two"]', 2),
        ],
    )
    old.commit()
    old.close()

    store = SQLiteMemoryMediaStateStore(path)
    assert store.get('legacy', 'one').record.content == 'new'
    changed = replace(store.get('legacy', 'two').record, content='after-migration')
    store.save(changed, store.get('legacy', 'two').retention)
    store.close()

    restarted = SQLiteMemoryMediaStateStore(path)
    assert restarted.get('legacy', 'one').record.content == 'after-migration'
    assert not restarted._connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='guanjia_memory_media_states_legacy'"
    ).fetchone()
    restarted.close()


def test_legacy_media_migration_conflict_rolls_back_and_keeps_source_tables(tmp_path) -> None:
    path = tmp_path / 'legacy-conflict.db'
    old = sqlite3.connect(path)
    old.execute(
        'CREATE TABLE guanjia_memory_media_states '
        '(record_key TEXT, media_id TEXT, record_payload TEXT, reasons_payload TEXT, revision INTEGER)'
    )
    from guanjia.memory.repository import _encode
    old.executemany(
        'INSERT INTO guanjia_memory_media_states VALUES(?, ?, ?, ?, ?)',
        [
            ('legacy', 'one', _encode(record('legacy', 'left')), '["one"]', 1),
            ('legacy', 'two', _encode(record('legacy', 'right')), '["two"]', 1),
        ],
    )
    old.commit()
    old.close()
    try:
        SQLiteMemoryMediaStateStore(path)
    except ValueError as error:
        assert str(error) == 'legacy media record revisions conflict'
    else:
        raise AssertionError('same revision divergent legacy payload must reject migration')
    check = sqlite3.connect(path)
    assert check.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='guanjia_memory_media_states'"
    ).fetchone()
    assert check.execute('SELECT COUNT(*) FROM guanjia_memory_media_states').fetchone()[0] == 2
    check.close()
