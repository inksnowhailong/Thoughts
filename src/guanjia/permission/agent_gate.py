"""Agent 外部沙箱与核心授权的双层闸门。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import re
from typing import Iterable, Protocol

from .authorizer import Authorizer
from .confirmations import ConfirmationTodo
from .contracts import AuthorizationRequest, PermissionEvaluation, PermissionOutcome, Reversibility, required, utc
from .grants import Grant
from .roles import Role


_IDENTIFIER_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_HEX_64_PATTERN = re.compile(r'[0-9a-f]{64}')
_MAX_PROOF_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class SandboxDecision:
    """A cryptographically signed, request-bound external sandbox decision."""

    request_id: str
    actor_id: str
    actor_role: Role
    actor_sources: tuple[str, ...]
    task_id: str | None
    session_id: str
    intent: str
    capability: str
    object_scope: str
    risk_scope: str
    within_intent_scope: bool
    reversibility: Reversibility
    provenance_summary: str
    action_fingerprint: str
    allowed: bool
    issued_at: datetime
    expires_at: datetime
    issuer: str
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, 'request_id'), (self.actor_id, 'actor_id'),
            (self.session_id, 'session_id'), (self.intent, 'intent'),
            (self.capability, 'capability'), (self.object_scope, 'object_scope'),
            (self.risk_scope, 'risk_scope'), (self.provenance_summary, 'provenance_summary'),
            (self.action_fingerprint, 'action_fingerprint'),
        ):
            required(value, name)
        if self.task_id is not None:
            required(self.task_id, 'task_id')
        if type(self.actor_role) is not Role:
            raise ValueError('actor_role must be a Role')
        if type(self.actor_sources) is not tuple or not self.actor_sources:
            raise ValueError('actor_sources must be a non-empty tuple')
        for source in self.actor_sources:
            required(source, 'actor source')
        if type(self.allowed) is not bool:
            raise ValueError('allowed must be bool')
        if type(self.within_intent_scope) is not bool:
            raise ValueError('within_intent_scope must be bool')
        if type(self.reversibility) is not Reversibility:
            raise ValueError('reversibility must be a Reversibility')
        if _HEX_64_PATTERN.fullmatch(self.action_fingerprint) is None:
            raise ValueError('action_fingerprint must be lowercase 64-hex')
        if _IDENTIFIER_PATTERN.fullmatch(self.issuer) is None:
            raise ValueError('issuer must be 1-64 URL-safe identifier characters')
        if _IDENTIFIER_PATTERN.fullmatch(self.key_id) is None:
            raise ValueError('key_id must be 1-64 URL-safe identifier characters')
        if _HEX_64_PATTERN.fullmatch(self.signature) is None:
            raise ValueError('signature must be lowercase sha256 hex')
        utc(self.issued_at, 'issued_at')
        utc(self.expires_at, 'expires_at')
        if self.expires_at <= self.issued_at:
            raise ValueError('proof expiry must follow issue time')
        if self.expires_at - self.issued_at > _MAX_PROOF_TTL:
            raise ValueError('proof TTL exceeds maximum')

    def matches(self, request: AuthorizationRequest) -> bool:
        """Require every authorization-relevant request field to match the proof."""
        return (
            self.allowed and self.issued_at <= request.now < self.expires_at and
            self.request_id == request.request_id and
            self.actor_id == request.actor.actor_id and self.actor_role is request.actor.role and
            self.actor_sources == request.actor.sources and self.task_id == request.task_id and
            self.session_id == request.session_id and self.intent == request.intent and
            self.capability == request.capability and self.object_scope == request.object_scope and
            self.risk_scope == request.risk_scope and
            self.within_intent_scope is request.within_intent_scope and
            self.reversibility is request.reversibility and
            self.provenance_summary == request.provenance_summary and
            self.action_fingerprint == request.action_fingerprint
        )


SandboxProof = SandboxDecision


class SandboxVerifier(Protocol):
    def verifies(self, decision: SandboxDecision, request: AuthorizationRequest) -> bool: ...


class TestOnlyMatchingSandboxVerifier:
    """Test-only verifier; production AgentGate construction rejects it."""

    def verifies(self, decision: SandboxDecision, request: AuthorizationRequest) -> bool:
        return decision.matches(request)


class HmacSandboxVerifier:
    """Verifies HMAC-SHA256 proofs from configured sandbox issuers."""

    def __init__(self, keys: dict[str, tuple[str, bytes]]) -> None:
        self._keys: dict[str, tuple[str, bytes]] = {}
        for key_id, key in keys.items():
            if _IDENTIFIER_PATTERN.fullmatch(key_id) is None:
                raise ValueError('key_id must be 1-64 URL-safe identifier characters')
            if type(key) is not tuple or len(key) != 2:
                raise ValueError('key must be an (expected_issuer, secret) tuple')
            issuer, secret = key
            if _IDENTIFIER_PATTERN.fullmatch(issuer) is None:
                raise ValueError('issuer must be 1-64 URL-safe identifier characters')
            if type(secret) is not bytes or len(secret) < 32:
                raise ValueError('sandbox HMAC secret must be at least 32 bytes')
            self._keys[key_id] = (issuer, secret)

    def verifies(self, decision: SandboxDecision, request: AuthorizationRequest) -> bool:
        key = self._keys.get(decision.key_id)
        if key is None or not decision.matches(request):
            return False
        expected_issuer, secret = key
        if not hmac.compare_digest(decision.issuer, expected_issuer):
            return False
        return hmac.compare_digest(decision.signature, _signature(secret, decision))


def _payload(decision: SandboxDecision) -> bytes:
    """Encode a stable, signed payload without accepting caller-supplied mappings."""
    values = {name: getattr(decision, name) for name in (
        'request_id', 'actor_id', 'actor_role', 'actor_sources', 'task_id', 'session_id',
        'intent', 'capability', 'object_scope', 'risk_scope', 'within_intent_scope',
        'reversibility', 'provenance_summary', 'action_fingerprint', 'allowed',
        'issued_at', 'expires_at', 'issuer', 'key_id',
    )}
    values['actor_role'] = decision.actor_role.value
    values['actor_sources'] = list(decision.actor_sources)
    values['reversibility'] = decision.reversibility.value
    values['issued_at'] = _canonical_utc_timestamp(decision.issued_at)
    values['expires_at'] = _canonical_utc_timestamp(decision.expires_at)
    return json.dumps(values, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _canonical_utc_timestamp(value: datetime) -> str:
    """Use fixed UTC `Z` timestamps with six-digit microseconds."""
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _signature(secret: bytes, decision: SandboxDecision) -> str:
    return hmac.new(secret, _payload(decision), sha256).hexdigest()


class AgentGate:
    def __init__(self, authorizer: Authorizer, verifier: SandboxVerifier) -> None:
        if isinstance(verifier, TestOnlyMatchingSandboxVerifier):
            raise ValueError('TestOnlyMatchingSandboxVerifier cannot be used by AgentGate')
        self._authorizer = authorizer
        self._verifier = verifier

    def authorize(
        self,
        request: AuthorizationRequest,
        sandbox_decision: SandboxDecision,
        grants: Iterable[Grant] = (),
        todos: Iterable[ConfirmationTodo] = (),
    ) -> PermissionEvaluation:
        if type(sandbox_decision) is not SandboxDecision:
            raise ValueError('sandbox_decision must be a SandboxDecision')
        core = self._authorizer.authorize(request, grants, todos)
        if core.outcome is not PermissionOutcome.ALLOW:
            return core
        try:
            verified = self._verifier.verifies(sandbox_decision, request)
        except Exception:
            verified = False
        if verified is not True:
            return PermissionEvaluation(PermissionOutcome.DENY, 'external sandbox denied')
        return core
