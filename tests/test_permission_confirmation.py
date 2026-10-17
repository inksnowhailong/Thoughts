from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier

from guanjia.permission import (
    ActorEvidence,
    AuthorizationService,
    Authorizer,
    AuthorizationRequest as _AuthorizationRequest,
    ConfirmationTodo,
    Decision,
    InMemoryPermissionStore,
    IdempotencyConflict,
    MigrationRequired,
    PermissionOutcome,
    PermissionDecision,
    Reversibility,
    Role,
    RolePolicy,
    SQLitePermissionStore,
)

def AuthorizationRequest(*args, **kwargs):
    return _AuthorizationRequest(*args, action_fingerprint=kwargs.pop('action_fingerprint', 'a' * 64), **kwargs)


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
owner = ActorEvidence('owner', Role.OWNER, ('wechat:owner',))
request = AuthorizationRequest(
    request_id='sensitive-1',
    actor=owner,
    session_id='session-1',
    task_id='task-1',
    intent='publish release notes',
    capability='message.public_send',
    object_scope='project-news',
    risk_scope='public-representation',
    within_intent_scope=True,
    reversibility=Reversibility.REVERSIBLE,
    provenance_summary='user request',
    now=now,
)
todo = ConfirmationTodo.from_request('todo-1', request, expires_at=now + timedelta(minutes=3))
try:
    replace(todo, action_fingerprint='A' * 64)
except ValueError:
    check(True, 'confirmation todo fingerprint is strictly lowercase 64-hex')
else:
    check(False, 'confirmation todo fingerprint is strictly lowercase 64-hex')
policy = Authorizer(RolePolicy({Role.OWNER: frozenset({'message.public_send'})}, {Role.OWNER: False}))
check(policy.authorize(request, todos=(todo,)).outcome == 'confirmation_required', 'critical request creates a shared confirmation todo')
store = InMemoryPermissionStore()
store.save_todo(todo)
check(
    store.active_todos_for(owner.actor_id, owner.role, request.session_id, request.task_id, now) == (todo,),
    'active todo query binds actor role session and task',
)
approved = store.resolve('todo-1', owner, Decision.APPROVE, now)
check(approved.status == 'approved', 'same actor can approve from another channel')
check(
    policy.authorize(request, todos=store.decisions_for(request)).outcome == 'allow',
    'stored approved todo closes authorization loop',
)
service_store = InMemoryPermissionStore()
service = AuthorizationService(policy, service_store)
pending_decision = service.decide(request)
check(pending_decision.todo_id == 'confirmation:sensitive-1', 'service creates stable request-bound todo id')
check(
    service.decide(request).todo_id == pending_decision.todo_id,
    'same pending request reuses its original todo and expiry',
)
try:
    service.decide(AuthorizationRequest(
        'sensitive-1', owner, 'session-1', 'task-1', 'publish release notes', 'message.public_send',
        'project-news', 'public-representation', True, Reversibility.REVERSIBLE, 'user request', now,
        action_fingerprint='b' * 64,
    ))
except IdempotencyConflict:
    check(True, 'pending request id rejects different permission semantics')
else:
    check(False, 'pending request id rejects different permission semantics')
service_store.resolve(pending_decision.todo_id, owner, Decision.APPROVE, now)
check(
    service.decide(request).outcome is PermissionOutcome.ALLOW,
    'approved request re-enters authorizer and closes the authorization loop',
)
try:
    PermissionDecision(PermissionOutcome.ALLOW, 'already allowed', 'todo-1')
except ValueError:
    check(True, 'allow decisions cannot expose a confirmation todo')
else:
    check(False, 'allow decisions cannot expose a confirmation todo')
try:
    PermissionDecision(PermissionOutcome.CONFIRMATION_REQUIRED, 'missing todo')
except ValueError:
    check(True, 'public confirmation decisions require a todo')
else:
    check(False, 'public confirmation decisions require a todo')
try:
    AuthorizationService(policy, InMemoryPermissionStore(), ttl=timedelta(0))
except ValueError:
    check(True, 'service requires a positive timedelta ttl')
else:
    check(False, 'service requires a positive timedelta ttl')
try:
    AuthorizationService(policy, InMemoryPermissionStore(), todo_id_factory=lambda _: '').decide(
        AuthorizationRequest(
            'invalid-factory', owner, 'session-1', 'task-1', 'publish release notes',
            'message.public_send', 'project-news', 'public-representation', True,
            Reversibility.REVERSIBLE, 'user request', now,
        )
    )
except ValueError:
    check(True, 'service rejects invalid todo factory output')
else:
    check(False, 'service rejects invalid todo factory output')
todo_ids = iter(('custom-first', 'custom-second'))
custom_service = AuthorizationService(
    policy, InMemoryPermissionStore(), todo_id_factory=lambda _: next(todo_ids),
)
first_custom = custom_service.decide(AuthorizationRequest(
    'custom-request', owner, 'session-1', 'task-1', 'publish release notes',
    'message.public_send', 'project-news', 'public-representation', True,
    Reversibility.REVERSIBLE, 'user request', now,
))
check(
    custom_service.decide(AuthorizationRequest(
        'custom-request', owner, 'session-1', 'task-1', 'publish release notes',
        'message.public_send', 'project-news', 'public-representation', True,
        Reversibility.REVERSIBLE, 'user request', now,
    )).todo_id == first_custom.todo_id,
    'request index prevents custom factories from creating a second todo',
)
check(
    policy.authorize(AuthorizationRequest(
        'sensitive-2', owner, 'session-1', 'other-task', 'publish release notes', 'message.public_send',
        'project-news', 'public-representation', True, Reversibility.REVERSIBLE, 'user request', now,
    ), todos=(approved,)).outcome == 'confirmation_required',
    'confirmation is task-bound and cannot leak to another task',
)
same_role_other_actor = ActorEvidence('other-owner', Role.OWNER, ('wechat:other',))
try:
    todo.resolve(same_role_other_actor, Decision.APPROVE, now)
except ValueError:
    check(True, 'same role different actor cannot confirm')
else:
    check(False, 'same role different actor cannot confirm')
deferred = todo.resolve(owner, Decision.DEFER, now)
check(deferred.status == 'deferred', 'structured defer is retained as a todo state')
try:
    todo.resolve(owner, '允许', now)  # type: ignore[arg-type]
except ValueError:
    check(True, 'natural language is rejected in permission layer')
else:
    check(False, 'natural language is rejected in permission layer')

check(
    store.active_todos_for(owner.actor_id, owner.role, request.session_id, request.task_id, now) == (),
    'approved todo leaves active UI query',
)
try:
    approved.resolve(owner, Decision.REJECT, now)
except ValueError:
    check(True, 'approved todo cannot transition again')
else:
    check(False, 'approved todo cannot transition again')

noncritical = AuthorizationRequest(
    'scope-1', owner, 'session-2', None, 'organize', 'file.organize', 'C:/Photos', 'reversible',
    False, Reversibility.IRREVERSIBLE, 'user request', now,
)
scope_authorizer = Authorizer(RolePolicy({Role.OWNER: frozenset({'file.organize'})}, {Role.OWNER: False}))
scope_todo = ConfirmationTodo.from_request('scope-todo', noncritical, now + timedelta(minutes=3))
check(
    scope_authorizer.authorize(noncritical, todos=(scope_todo,)).outcome is PermissionOutcome.CONFIRMATION_REQUIRED,
    'noncritical out-of-scope action requires confirmation',
)
check(
    scope_authorizer.authorize(noncritical, todos=(scope_todo.resolve(owner, Decision.APPROVE, now),)).outcome is PermissionOutcome.ALLOW,
    'approved todo closes scope and irreversibility confirmation',
)
forbidden = AuthorizationRequest(
    'forbidden-1', owner, 'session-3', None, 'forbidden', 'file.forbidden', 'C:/Secret', 'reversible',
    False, Reversibility.REVERSIBLE, 'user request', now,
)
forbidden_todo = ConfirmationTodo.from_request('forbidden-todo', forbidden, now + timedelta(minutes=3)).resolve(owner, Decision.APPROVE, now)
check(
    scope_authorizer.authorize(forbidden, todos=(forbidden_todo,)).outcome is PermissionOutcome.DENY,
    'role-forbidden capability cannot be confirmation-bypassed',
)
expired_todo = ConfirmationTodo.from_request('expired', request, now - timedelta(seconds=1))
check(expired_todo.resolve(owner, Decision.APPROVE, now).status.value == 'expired', 'expired todo takes precedence')
try:
    ConfirmationTodo.from_request('naive', request, datetime(2026, 10, 16))
except ValueError:
    check(True, 'todo expiry must be aware UTC')
else:
    check(False, 'todo expiry must be aware UTC')

race_todo = ConfirmationTodo.from_request('race', request, now + timedelta(minutes=3))
race_store = InMemoryPermissionStore()
race_store.save_todo(race_todo)
def resolve_race(decision):
    try:
        return race_store.resolve('race', owner, decision, now)
    except ValueError:
        return None
with ThreadPoolExecutor(max_workers=2) as workers:
    outcomes = list(workers.map(resolve_race, (Decision.APPROVE, Decision.REJECT)))
check(sum(item is not None for item in outcomes) == 1, 'concurrent opposite decisions have one winner')
check(
    race_store.active_todos_for(
        owner.actor_id, owner.role, request.session_id, request.task_id, now
    ) == () and len(race_store.decisions_for(request)) in (0, 1),
    'terminal todo cannot be flipped by losing decision',
)
try:
    store.resolve('missing', owner, Decision.APPROVE, now)
except KeyError:
    check(True, 'resolving unknown todo raises KeyError')
else:
    check(False, 'resolving unknown todo raises KeyError')

same_request_store = InMemoryPermissionStore()
same_request_store.save_todo(ConfirmationTodo.from_request('first-request-todo', request, now + timedelta(minutes=3)))
try:
    same_request_store.save_todo(ConfirmationTodo.from_request('second-request-todo', request, now + timedelta(minutes=3)))
except IdempotencyConflict:
    check(True, 'one request cannot be bound to multiple todo identifiers')
else:
    check(False, 'one request cannot be bound to multiple todo identifiers')

canonical_row = (
    'todo', 'request', 'actor', 'owner', 'session', 'task', 'intent', 'capability',
    'object', 'risk', 'a' * 64, 'reversible', 1,
    (now + timedelta(minutes=3)).isoformat(), 'pending', 1,
)
for text_index in (0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 13, 14):
    for polluted_value in (b'polluted', 7):
        row = list(canonical_row)
        row[text_index] = polluted_value
        try:
            SQLitePermissionStore._row_to_todo(tuple(row))
        except ValueError:
            continue
        check(False, 'SQLite rejects BLOB and integer pollution in every text field')
check(True, 'SQLite rejects BLOB and integer pollution in every text field')
for polluted_task in (b'polluted', 7):
    row = list(canonical_row)
    row[5] = polluted_task
    try:
        SQLitePermissionStore._row_to_todo(tuple(row))
    except ValueError:
        continue
    check(False, 'SQLite task_id only accepts null or text')
check(True, 'SQLite task_id only accepts null or text')
for index, polluted_values in ((12, (True, 2, b'polluted')), (15, (True, 0, 1.0, b'polluted'))):
    for polluted_value in polluted_values:
        row = list(canonical_row)
        row[index] = polluted_value
        try:
            SQLitePermissionStore._row_to_todo(tuple(row))
        except ValueError:
            continue
        check(False, 'SQLite integer fields require strict valid integers')
check(True, 'SQLite integer fields require strict valid integers')
for invalid_timestamp in ('2026-10-16T00:00:00Z', '2026-10-16T08:00:00+08:00'):
    row = list(canonical_row)
    row[13] = invalid_timestamp
    try:
        SQLitePermissionStore._row_to_todo(tuple(row))
    except ValueError:
        continue
    check(False, 'SQLite timestamps are canonical aware UTC')
check(True, 'SQLite timestamps are canonical aware UTC')

with TemporaryDirectory() as directory:
    sqlite_path = Path(directory) / 'nested' / 'permission.sqlite3'
    first_store = SQLitePermissionStore(sqlite_path)
    second_store = SQLitePermissionStore(sqlite_path)
    sqlite_todo = ConfirmationTodo.from_request('sqlite-race', request, now + timedelta(minutes=3))

    def create_with(store):
        store.save_todo(sqlite_todo)
        return store.todo(sqlite_todo.todo_id)

    with ThreadPoolExecutor(max_workers=2) as workers:
        created = list(workers.map(create_with, (first_store, second_store)))
    check(created == [sqlite_todo, sqlite_todo], 'two SQLite instances create-or-compare the same todo')

    def resolve_sql(decision):
        try:
            return second_store.resolve('sqlite-race', owner, decision, now)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        resolved = list(workers.map(resolve_sql, (Decision.APPROVE, Decision.REJECT)))
    check(sum(item is not None for item in resolved) == 1, 'SQLite resolve uses a versioned compare-and-swap transition')

    taskless_request = AuthorizationRequest(
        'sqlite-taskless', owner, 'session-1', None, 'publish release notes', 'message.public_send',
        'project-news', 'public-representation', True, Reversibility.REVERSIBLE, 'user request', now,
    )
    taskless_todo = ConfirmationTodo.from_request('sqlite-taskless', taskless_request, now + timedelta(minutes=3))
    first_store.save_todo(taskless_todo)
    first_store.resolve(taskless_todo.todo_id, owner, Decision.APPROVE, now)
    check(
        first_store.decisions_for(taskless_request) == (taskless_todo.resolve(owner, Decision.APPROVE, now),),
        'SQLite decisions query is NULL-safe for taskless requests',
    )
    check(
        first_store.decisions_for(request) == tuple(
            todo for todo in first_store.decisions_for(request) if todo.task_id == request.task_id
        ),
        'SQLite decisions query exactly binds non-null task ids',
    )

    concurrent_request = AuthorizationRequest(
        'sqlite-concurrent-service', owner, 'session-1', 'task-1', 'publish release notes',
        'message.public_send', 'project-news', 'public-representation', True,
        Reversibility.REVERSIBLE, 'user request', now,
    )
    factory_barrier = Barrier(2)
    service_one = AuthorizationService(
        policy, first_store, todo_id_factory=lambda _: (factory_barrier.wait(), 'winner-one')[1],
    )
    service_two = AuthorizationService(
        policy, second_store, todo_id_factory=lambda _: (factory_barrier.wait(), 'winner-two')[1],
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        concurrent_decisions = list(workers.map(
            lambda current_service: current_service.decide(concurrent_request),
            (service_one, service_two),
        ))
    check(
        len({decision.todo_id for decision in concurrent_decisions}) == 1,
        'concurrent non-deterministic factories return the request-index winner',
    )
    first_store.close()
    second_store.close()
    restarted_store = SQLitePermissionStore(sqlite_path)
    check(
        restarted_store.todo('sqlite-race') == next(item for item in resolved if item is not None),
        'SQLite restart reads the latest field-by-field durable todo state',
    )
    restarted_store.close()
    restarted_store.close()
    try:
        restarted_store.todo('sqlite-race')
    except RuntimeError:
        check(True, 'closed SQLite stores reject further operations')
    else:
        check(False, 'closed SQLite stores reject further operations')

    connection = sqlite3.connect(sqlite_path)
    connection.execute('UPDATE permission_todos SET within_intent_scope=2 WHERE todo_id=?', ('sqlite-race',))
    connection.commit()
    connection.close()
    dirty_store = SQLitePermissionStore(sqlite_path)
    try:
        dirty_store.todo('sqlite-race')
    except ValueError:
        check(True, 'SQLite rejects dirty boolean and durable row values')
    else:
        check(False, 'SQLite rejects dirty boolean and durable row values')
    dirty_store.close()

with TemporaryDirectory() as directory:
    legacy_path = Path(directory) / 'legacy.sqlite3'
    legacy_connection = sqlite3.connect(legacy_path)
    legacy_connection.execute('CREATE TABLE permission_todos (todo_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL)')
    legacy_connection.execute('INSERT INTO permission_todos VALUES (?, ?)', ('legacy-todo', 'legacy-actor'))
    legacy_connection.commit()
    legacy_connection.close()
    try:
        SQLitePermissionStore(legacy_path)
    except MigrationRequired:
        reopened = sqlite3.connect(legacy_path)
        preserved = reopened.execute('SELECT todo_id, actor_id FROM permission_todos').fetchone()
        version = reopened.execute('PRAGMA user_version').fetchone()[0]
        reopened.close()
        check(
            preserved == ('legacy-todo', 'legacy-actor') and version == 0,
            'unsafe legacy schemas retain their data and schema version',
        )
    else:
        check(False, 'unsafe legacy schemas require an explicit migration')

print('ALL PASS test_permission_confirmation')
