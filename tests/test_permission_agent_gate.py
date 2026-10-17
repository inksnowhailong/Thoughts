from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac

from guanjia.permission import (
    ActorEvidence, AgentGate, Authorizer, AuthorizationRequest, HmacSandboxVerifier,
    PermissionOutcome, Reversibility, Role, RolePolicy, SandboxDecision,
)
from guanjia.permission.contracts import PermissionEvaluation
from guanjia.permission.agent_gate import TestOnlyMatchingSandboxVerifier, _payload


SECRET = b'test-secret-material-must-be-at-least-32-bytes'
NOW = datetime(2026, 10, 16, 12, 0, 0, 123456, tzinfo=timezone.utc)
VERIFIER = HmacSandboxVerifier({'test-key': ('sandbox', SECRET)})


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


def request(**changes: object) -> AuthorizationRequest:
    values: dict[str, object] = {
        'request_id': 'agent-1',
        'actor': ActorEvidence('owner', Role.OWNER, ('wechat:owner',)),
        'session_id': 'session-1',
        'task_id': 'task-1',
        'intent': 'sort files',
        'capability': 'file.organize',
        'object_scope': 'C:/Photos',
        'risk_scope': 'reversible',
        'within_intent_scope': True,
        'reversibility': Reversibility.REVERSIBLE,
        'provenance_summary': 'user request',
        'now': NOW,
        'action_fingerprint': 'a' * 64,
    }
    values.update(changes)
    return AuthorizationRequest(**values)


def signed_proof(for_request: AuthorizationRequest, **changes: object) -> SandboxDecision:
    values: dict[str, object] = {
        'request_id': for_request.request_id,
        'actor_id': for_request.actor.actor_id,
        'actor_role': for_request.actor.role,
        'actor_sources': for_request.actor.sources,
        'task_id': for_request.task_id,
        'session_id': for_request.session_id,
        'intent': for_request.intent,
        'capability': for_request.capability,
        'object_scope': for_request.object_scope,
        'risk_scope': for_request.risk_scope,
        'within_intent_scope': for_request.within_intent_scope,
        'reversibility': for_request.reversibility,
        'provenance_summary': for_request.provenance_summary,
        'action_fingerprint': for_request.action_fingerprint,
        'allowed': True,
        'issued_at': NOW - timedelta(seconds=1),
        'expires_at': NOW + timedelta(minutes=1),
        'issuer': 'sandbox',
        'key_id': 'test-key',
    }
    values.update(changes)
    unsigned = SandboxDecision(signature='0' * 64, **values)
    signature = hmac.new(SECRET, _payload(unsigned), sha256).hexdigest()
    return SandboxDecision(signature=signature, **values)


authorizer = Authorizer(RolePolicy({Role.OWNER: frozenset({'file.organize'})}, {Role.OWNER: False}))
gate = AgentGate(authorizer, VERIFIER)
approved_request = request()
proof = signed_proof(approved_request)

allowed = gate.authorize(approved_request, proof)
check(isinstance(allowed, PermissionEvaluation) and allowed.outcome is PermissionOutcome.ALLOW, 'valid signed proof allows')
check('TestOnlyMatchingSandboxVerifier' not in __import__('guanjia.permission', fromlist=['*']).__all__, 'test verifier is not exported')
try:
    AgentGate(authorizer, TestOnlyMatchingSandboxVerifier())
except ValueError:
    check(True, 'test-only verifier is rejected by AgentGate')
else:
    check(False, 'test-only verifier is rejected by AgentGate')

for field, value in (
    ('request_id', 'agent-2'),
    ('actor_id', 'another-owner'),
    ('actor_role', Role.VISITOR),
    ('actor_sources', ('camera',)),
    ('task_id', 'task-2'),
    ('session_id', 'session-2'),
    ('intent', 'delete files'),
    ('capability', 'file.delete'),
    ('object_scope', 'C:/Other'),
    ('risk_scope', 'irreversible'),
    ('within_intent_scope', False),
    ('reversibility', Reversibility.IRREVERSIBLE),
    ('provenance_summary', 'different request'),
    ('action_fingerprint', 'b' * 64),
    ('allowed', False),
):
    check(
        gate.authorize(approved_request, replace(proof, **{field: value})).outcome is PermissionOutcome.DENY,
        f'tampered {field} is denied',
    )

sandbox_denied = gate.authorize(approved_request, replace(proof, issuer='forged'))
check(
    isinstance(sandbox_denied, PermissionEvaluation)
    and sandbox_denied.outcome is PermissionOutcome.DENY
    and sandbox_denied.reason == 'external sandbox denied',
    'forged issuer is denied',
)
check(gate.authorize(approved_request, replace(proof, key_id='other-key')).outcome is PermissionOutcome.DENY, 'forged key is denied')
check(gate.authorize(approved_request, replace(proof, signature='f' * 64)).outcome is PermissionOutcome.DENY, 'forged signature is denied')
check(
    gate.authorize(approved_request, signed_proof(approved_request, issued_at=NOW + timedelta(seconds=1), expires_at=NOW + timedelta(minutes=1))).outcome is PermissionOutcome.DENY,
    'future proof is denied',
)
check(
    gate.authorize(approved_request, signed_proof(approved_request, issued_at=NOW - timedelta(minutes=2), expires_at=NOW - timedelta(seconds=1))).outcome is PermissionOutcome.DENY,
    'expired proof is denied',
)


class RaisingVerifier:
    def verifies(self, decision: SandboxDecision, authorization_request: AuthorizationRequest) -> bool:
        raise RuntimeError('sandbox unavailable')


class TruthyVerifier:
    def verifies(self, decision: SandboxDecision, authorization_request: AuthorizationRequest) -> bool:
        return 1  # type: ignore[return-value]


check(
    AgentGate(authorizer, RaisingVerifier()).authorize(approved_request, proof).outcome is PermissionOutcome.DENY,
    'verifier exception fails closed',
)
check(
    AgentGate(authorizer, TruthyVerifier()).authorize(approved_request, proof).outcome is PermissionOutcome.DENY,
    'non-boolean verifier result fails closed',
)
visitor_request = request(actor=ActorEvidence('visitor', Role.VISITOR, ('camera',)))
check(gate.authorize(visitor_request, proof).outcome is PermissionOutcome.DENY, 'core deny cannot be bypassed')

print('ALL PASS test_permission_agent_gate')
