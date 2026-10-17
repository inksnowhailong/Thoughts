from datetime import datetime, timedelta, timezone

from guanjia.permission import (
    ActorEvidence, Authorizer, AuthorizationRequest as _AuthorizationRequest, Grant, Reversibility, Role, RolePolicy,
)

def AuthorizationRequest(*args, **kwargs):
    return _AuthorizationRequest(*args, action_fingerprint='a' * 64, **kwargs)


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
owner = ActorEvidence('owner', Role.OWNER, ('wechat:owner',))
request = AuthorizationRequest(
    request_id='request-1',
    actor=owner,
    session_id='session-1',
    task_id='task-1',
    intent='sort photos',
    capability='file.move',
    object_scope='C:/Photos',
    risk_scope='reversible',
    within_intent_scope=True,
    reversibility=Reversibility.REVERSIBLE,
    provenance_summary='user request',
    now=now,
)
policy = Authorizer(RolePolicy({
    Role.OWNER: frozenset({'file.move'}),
    Role.TRUSTED_FAMILY: frozenset({'media.play'}),
}, {Role.OWNER: False}))
grant = Grant.for_task(request, expires_at=now + timedelta(minutes=5))
check(policy.authorize(request, grants=(grant,)).outcome == 'allow', 'task grant allows matching action')
check(
    not grant.matches(AuthorizationRequest(
        'request-2', owner, 'session-1', 'task-2', 'sort photos', 'file.move', 'C:/Photos', 'reversible',
        True, Reversibility.REVERSIBLE, 'user request', now,
    )),
    'task grant is exactly bound to its task',
)
check(
    not grant.matches(AuthorizationRequest(
        'request-3', owner, 'session-1', 'task-1', 'sort photos', 'file.move', 'C:/Secrets', 'reversible',
        True, Reversibility.REVERSIBLE, 'user request', now,
    )),
    'grant cannot expand object scope',
)
expired = Grant.for_task(request, expires_at=now - timedelta(seconds=1))
check(not expired.matches(request), 'expired grant cannot authorize action')
half_open = Grant.for_task(request, expires_at=now)
check(not half_open.matches(request), 'grant expiry is half-open at the exact expiry instant')

rule = Grant.long_lived(
    grant_id='rule-1',
    role=Role.TRUSTED_FAMILY,
    capability='media.play',
    object_scope='living-room',
    risk_scope='reversible',
    expires_at=now + timedelta(days=30),
)
family_request = AuthorizationRequest(
    request_id='request-4',
    actor=ActorEvidence('family', Role.TRUSTED_FAMILY, ('voice:family',)),
    session_id='session-2',
    task_id='task-4',
    intent='play music',
    capability='media.play',
    object_scope='living-room',
    risk_scope='reversible',
    within_intent_scope=True,
    reversibility=Reversibility.REVERSIBLE,
    provenance_summary='user request',
    now=now,
)
check(policy.authorize(family_request, grants=(rule,)).outcome == 'allow', 'long-lived rule matches role and scope')

critical_request = AuthorizationRequest(
    'critical-grant', owner, 'session-1', 'task-1', 'transfer', 'finance.transfer', 'bank:one', 'financial',
    True, Reversibility.REVERSIBLE, 'user request', now,
)
critical_grant = Grant.for_task(critical_request, now + timedelta(minutes=5))
critical_authorizer = Authorizer(RolePolicy({Role.OWNER: frozenset({'finance.transfer'})}, {Role.OWNER: False}))
check(
    critical_authorizer.authorize(critical_request, grants=(critical_grant,)).outcome == 'confirmation_required',
    'critical request never consumes matching grant',
)
try:
    Grant.for_task(request, datetime(2026, 10, 16))
except ValueError:
    check(True, 'grant expiry must be aware UTC')
else:
    check(False, 'grant expiry must be aware UTC')

print('ALL PASS test_permission_grants')
