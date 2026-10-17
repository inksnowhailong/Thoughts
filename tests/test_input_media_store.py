from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from guanjia.input.media_store import MediaStore


class AllowAll:
    def authorize(self, media_ref, access_context):
        return access_context == 'test'


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
with TemporaryDirectory() as temporary:
    store = MediaStore(temporary, AllowAll())
    first = store.ingest_bytes(b'abc', 'audio/wav', reference_id='input-1')
    second = store.ingest_bytes(b'abc', 'audio/wav', reference_id='input-2')
    check(first.sha256 == second.sha256, 'CAS deduplicates equal content')
    with store.open_media(first.media_id, access_context='test') as stream:
        check(stream.read() == b'abc', 'controlled reader returns stored bytes')
    try:
        store.open_media(first.media_id, access_context='other')
    except PermissionError:
        pass
    else:
        raise AssertionError('media reads require authorization')
    for invalid_metadata, invalid_expiry in (({'bad': {1}}, None), ({}, datetime(2026, 10, 16))):
        try:
            store.ingest_bytes(
                b'invalid',
                'audio/wav',
                metadata=invalid_metadata,
                expires_at=invalid_expiry,
            )
        except ValueError:
            pass
        else:
            raise AssertionError('invalid media metadata or timestamp must be rejected')
    check(store.release(first.media_id, 'input-1'), 'release existing reference')
    check(store.prune(now=now, max_bytes=0) == (), 'remaining reference prevents cleanup')
    store.release(first.media_id, 'input-2')
    check(store.prune(now=now, max_bytes=0) == (first.sha256,), 'unreferenced media pruned by capacity')
    store.close()
with TemporaryDirectory() as temporary:
    store = MediaStore(temporary, AllowAll())
    expired = store.ingest_bytes(
        b'expired', 'audio/wav', expires_at=now - timedelta(seconds=1)
    )
    capacity = store.ingest_bytes(b'capacity', 'audio/wav')
    retained = store.ingest_bytes(b'retained', 'audio/wav', reference_id='input-2')
    removed = store.prune(now=now, max_bytes=len(b'retained'))
    check(
        expired.sha256 in removed and capacity.sha256 in removed,
        'expired removal reduces total before capacity pruning',
    )
    with store.open_media(retained.media_id, access_context='test') as stream:
        check(stream.read() == b'retained', 'referenced media is never pruned')
    store.close()
with TemporaryDirectory() as temporary:
    def crash_on_move(phase):
        if phase == 'ingest_moved':
            raise RuntimeError('simulated crash')

    crashed = MediaStore(
        temporary, AllowAll(), operation_lease_seconds=1, fault_injector=crash_on_move
    )
    try:
        crashed.ingest_bytes(b'journaled', 'audio/wav')
    except RuntimeError:
        pass
    else:
        raise AssertionError('fault injection must interrupt ingest after move')
    crashed.close()
    recovered = MediaStore(temporary, AllowAll(), operation_lease_seconds=1)
    recovered.reconcile(now=datetime.now(timezone.utc) + timedelta(seconds=2))
    digest = __import__('hashlib').sha256(b'journaled').hexdigest()
    with recovered.open_media(digest, access_context='test') as stream:
        check(stream.read() == b'journaled', 'expired moved ingest resumes without sweep deletion')
    recovered.close()
with TemporaryDirectory() as temporary:
    seed = MediaStore(temporary, AllowAll(), operation_lease_seconds=1)
    media = seed.ingest_bytes(b'prune-race', 'audio/wav')
    seed.close()

    def crash_prune(phase):
        if phase == 'prune_moved':
            raise RuntimeError('simulated crash')

    live_now = datetime.now(timezone.utc)
    pruner = MediaStore(
        temporary, AllowAll(), operation_lease_seconds=3600, fault_injector=crash_prune
    )
    try:
        pruner.prune(now=live_now, max_bytes=0)
    except RuntimeError:
        pass
    pruner.close()
    retainer = MediaStore(temporary, AllowAll(), operation_lease_seconds=3600)
    retainer.retain(media.media_id, 'race-reference')
    retainer.close()
    recovered = MediaStore(temporary, AllowAll(), operation_lease_seconds=3600)
    recovered.reconcile(now=live_now + timedelta(seconds=3601))
    with recovered.open_media(media.media_id, access_context='test') as stream:
        check(stream.read() == b'prune-race', 'recovery restores tombstone retained concurrently')
    recovered.close()
with TemporaryDirectory() as temporary:
    store = MediaStore(temporary, AllowAll())
    try:
        orphan = store.quarantine / 'untracked.part'
        orphan.write_bytes(b'temp')
        check(orphan in store.list_orphan_temps(minimum_age_seconds=0), 'untracked temp is reported')
        try:
            store.cleanup_orphan_temps(minimum_age_seconds=0, acknowledge=False)
        except PermissionError:
            pass
        else:
            raise AssertionError('temporary cleanup requires acknowledgement')
        store.cleanup_orphan_temps(minimum_age_seconds=0, acknowledge=True)
        check(not orphan.exists(), 'acknowledged temporary cleanup is explicit')
    finally:
        store.close()
print('ALL PASS test_input_media_store')
