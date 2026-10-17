from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from guanjia.permission import ActorEvidence, AuthorizationRequest as _AuthorizationRequest, PermissionDecision, PermissionOutcome, Reversibility, Role
from guanjia.permission.audit import (
    LATCH_TTL,
    UI_DISPLAY_TTL,
    InMemoryPermissionAuditStore,
    PermissionAudit,
    SQLitePermissionAuditStore,
    action_fingerprint,
)

def AuthorizationRequest(*args, **kwargs):
    return _AuthorizationRequest(*args, action_fingerprint=kwargs.pop('action_fingerprint', 'a' * 64), **kwargs)


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


now = datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc)
owner = ActorEvidence('owner', Role.OWNER, ('wechat:owner',))
request = AuthorizationRequest(
    'request-1', owner, 'session-1', 'task-1', 'publish', 'message.public_send',
    'C:/private/release-notes.md', 'public', True, Reversibility.REVERSIBLE, 'user request', now,
)
decision = PermissionDecision(PermissionOutcome.CONFIRMATION_REQUIRED, 'needs confirmation', 'todo-1')
store = InMemoryPermissionAuditStore()
audit = PermissionAudit(store)
fingerprint = 'a' * 64
request = AuthorizationRequest(
    'request-1', owner, 'session-1', 'task-1', 'publish', 'message.public_send',
    'C:/private/release-notes.md', 'public', True, Reversibility.REVERSIBLE, 'user request', now,
    action_fingerprint=fingerprint,
)
record, created = audit.record(request, decision, 'todo-1', now)
check(created, 'permission audit records confirmation once')
check(
    record.display_expires_at - record.recorded_at == UI_DISPLAY_TTL
    and record.latch_expires_at - record.recorded_at == LATCH_TTL,
    'three minute UI display TTL and ten minute latch TTL use distinct fields',
)
check(
    audit.visible_in_ui(record, now + timedelta(minutes=2, seconds=59))
    and not audit.visible_in_ui(record, now + timedelta(minutes=3))
    and audit.latch_active(record, now + timedelta(minutes=9, seconds=59))
    and not audit.latch_active(record, now + timedelta(minutes=10)),
    'UI display expiration never changes latch expiration',
)
check(
    audit.confirmation_for(
        session_id='session-1', actor_id='owner', todo_id='todo-1', task_id='task-1',
        action_fingerprint=fingerprint, now=now,
    ) == record,
    'confirmation query binds session actor todo task and action fingerprint',
)
check(
    audit.confirmation_for(
        session_id='session-1', actor_id='owner', todo_id='todo-1', task_id='task-1',
        action_fingerprint=fingerprint, now=now + timedelta(minutes=10),
    ) is None,
    'expired latch cannot retrieve a confirmation even after UI expiry is independent',
)
for query in (
    {'session_id': 'other-session', 'actor_id': 'owner', 'todo_id': 'todo-1', 'task_id': 'task-1', 'action_fingerprint': fingerprint},
    {'session_id': 'session-1', 'actor_id': 'other-owner', 'todo_id': 'todo-1', 'task_id': 'task-1', 'action_fingerprint': fingerprint},
    {'session_id': 'session-1', 'actor_id': 'owner', 'todo_id': 'other-todo', 'task_id': 'task-1', 'action_fingerprint': fingerprint},
    {'session_id': 'session-1', 'actor_id': 'owner', 'todo_id': 'todo-1', 'task_id': 'other-task', 'action_fingerprint': fingerprint},
    {'session_id': 'session-1', 'actor_id': 'owner', 'todo_id': 'todo-1', 'task_id': 'task-1', 'action_fingerprint': 'b' * 64},
):
    check(audit.confirmation_for(now=now, **query) is None, 'scope mismatch cannot retrieve confirmation')

with ThreadPoolExecutor(max_workers=8) as workers:
    outcomes = list(workers.map(lambda _: audit.record(request, decision, 'todo-1', now)[1], range(16)))
check(sum(outcomes) == 0, 'concurrent duplicate audit records remain idempotent')

restarted = PermissionAudit(store)
_, after_restart = restarted.record(request, decision, 'todo-1', now)
check(not after_restart, 'reused persistent store preserves idempotency after adapter restart')
replayed, replayed_created = restarted.record(
    request, decision, 'todo-1', now + timedelta(seconds=1, microseconds=1),
)
check(not replayed_created and replayed == record, 'replay with a new now preserves the first persisted audit timestamps')
for invalid_decision, todo_id in (
    (PermissionDecision(PermissionOutcome.ALLOW, 'already allowed', 'todo-1'), 'todo-1'),
    (PermissionDecision(PermissionOutcome.CONFIRMATION_REQUIRED, 'wrong todo', 'other-todo'), 'todo-1'),
):
    try:
        audit.record(request, invalid_decision, todo_id, now)
    except ValueError:
        check(True, 'wrong decision outcome or todo binding is rejected')
    else:
        check(False, 'wrong decision outcome or todo binding is rejected')
check(action_fingerprint(request) == fingerprint, 'audit only reads the request fingerprint')

with TemporaryDirectory() as directory:
    sqlite_path = Path(directory) / 'nested' / 'permission-audit.sqlite3'
    first_store = SQLitePermissionAuditStore(sqlite_path)
    second_store = SQLitePermissionAuditStore(sqlite_path)
    first_audit = PermissionAudit(first_store)
    second_audit = PermissionAudit(second_store)
    race_decision = PermissionDecision(PermissionOutcome.CONFIRMATION_REQUIRED, 'needs confirmation', 'todo-sql-race')

    def record_sql(index: int):
        audit_instance = first_audit if index % 2 else second_audit
        return audit_instance.record(
            request, race_decision, 'todo-sql-race', now + timedelta(microseconds=index),
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        sql_results = list(workers.map(record_sql, range(16)))
    sql_record = next(item[0] for item in sql_results)
    check(sum(created for _, created in sql_results) == 1 and sqlite_path.exists(), 'two SQLite store instances create exactly one durable audit record')

    restarted_sql = PermissionAudit(SQLitePermissionAuditStore(sqlite_path))
    restored, restored_created = restarted_sql.record(
        request, race_decision, 'todo-sql-race', now + timedelta(seconds=1, microseconds=1),
    )
    check(not restored_created and restored == sql_record, 'SQLite restart returns the original persisted record and timestamps')

print('ALL PASS test_permission_audit')
