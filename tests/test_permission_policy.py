from datetime import datetime, timezone

from guanjia.permission import (
    ActorEvidence,
    Authorizer,
    AuthorizationRequest as _AuthorizationRequest,
    Reversibility,
    Role,
    RolePolicy,
)

def AuthorizationRequest(*args, **kwargs):
    return _AuthorizationRequest(*args, action_fingerprint='a' * 64, **kwargs)


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


policy = RolePolicy(
    allowed_capabilities={Role.OWNER: frozenset({'file.organize', 'finance.transfer'}), Role.TRUSTED_FAMILY: frozenset()},
    allow_irreversible={Role.OWNER: False},
)
authorizer = Authorizer(policy)
owner = ActorEvidence(actor_id='owner-1', role=Role.OWNER, sources=('wechat:owner',))
family = ActorEvidence(actor_id='family-1', role=Role.TRUSTED_FAMILY, sources=('voice:family',))
visitor = ActorEvidence(actor_id='visitor-1', role=Role.VISITOR, sources=('camera:visitor',))
self_loop = ActorEvidence(actor_id='self', role=Role.SELF_LOOP, sources=('internal',))

ordinary = AuthorizationRequest(
    request_id='auth-1',
    actor=owner,
    session_id='session-1',
    task_id='task-1',
    intent='organize photos',
    capability='file.organize',
    object_scope='C:/Photos',
    risk_scope='reversible',
    within_intent_scope=True,
    reversibility=Reversibility.REVERSIBLE,
    provenance_summary='user request',
    now=datetime(2026, 10, 16, tzinfo=timezone.utc),
)
check(authorizer.authorize(ordinary).outcome == 'allow', 'injected owner matrix allows declared capability')
check(authorizer.authorize(AuthorizationRequest(
    'auth-family', family, 'session-1', 'task-1', 'organize photos', 'file.organize', 'C:/Photos', 'reversible',
    True, Reversibility.REVERSIBLE, 'user request', ordinary.now,
)).outcome == 'deny', 'trusted family is not implicitly allowed')
check(authorizer.authorize(AuthorizationRequest(
    'auth-visitor', visitor, 'session-1', 'task-1', 'organize photos', 'file.organize', 'C:/Photos', 'reversible',
    True, Reversibility.REVERSIBLE, 'user request', ordinary.now,
)).outcome == 'deny', 'visitor is not implicitly trusted')
check(authorizer.authorize(AuthorizationRequest(
    'auth-self', self_loop, 'session-1', 'task-1', 'organize photos', 'file.organize', 'C:/Photos', 'reversible',
    True, Reversibility.REVERSIBLE, 'user request', ordinary.now,
)).outcome == 'deny', 'self loop cannot start external work')

critical = AuthorizationRequest(
    'auth-2', owner, 'session-1', 'task-1', 'organize photos',
    capability='finance.transfer',
    object_scope='C:/Photos',
    risk_scope='financial',
    within_intent_scope=True,
    reversibility=Reversibility.REVERSIBLE,
    provenance_summary='user request',
    now=ordinary.now,
)
critical_decision = authorizer.authorize(critical)
check(critical_decision.outcome == 'confirmation_required', 'critical capability always needs confirmation')
check('critical' in critical_decision.reason, 'decision describes critical confirmation reason')

outside = AuthorizationRequest(
    'auth-outside', owner, 'session-1', None, 'organize photos', 'file.organize', 'C:/Photos', 'reversible',
    False, Reversibility.REVERSIBLE, 'unrelated action', ordinary.now,
)
check(authorizer.authorize(outside).outcome == 'confirmation_required', 'out-of-scope action is not silently allowed')
check(outside.task_id is None, 'task id may be absent for direct action')

try:
    AuthorizationRequest(
        'bad-bool', owner, 'session-1', None, 'organize', 'file.organize', 'C:/Photos', 'reversible',
        1, Reversibility.REVERSIBLE, 'user request', ordinary.now,
    )
except ValueError:
    check(True, 'intent scope must be bool')
else:
    check(False, 'intent scope must be bool')
try:
    RolePolicy({Role.OWNER: frozenset({'file.organize'})}, {Role.OWNER: 1})
except ValueError:
    check(True, 'role matrix rejects non-boolean policy values')
else:
    check(False, 'role matrix rejects non-boolean policy values')
source_matrix = {Role.OWNER: {'file.organize'}}
frozen_policy = RolePolicy(source_matrix, {Role.OWNER: False})
source_matrix[Role.OWNER].add('system.destructive')
check(
    not frozen_policy.allows_capability(Role.OWNER, 'system.destructive'),
    'role matrix is copied and frozen from caller mutation',
)

print('ALL PASS test_permission_policy')
