"""权限领域的结构化输入、输出与枚举。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import re

from .roles import Role


class Decision(str, Enum):
    APPROVE = 'approve'
    REJECT = 'reject'
    DEFER = 'defer'
    UNCERTAIN = 'uncertain'


class Reversibility(str, Enum):
    REVERSIBLE = 'reversible'
    IRREVERSIBLE = 'irreversible'


class PermissionOutcome(str, Enum):
    ALLOW = 'allow'
    DENY = 'deny'
    CONFIRMATION_REQUIRED = 'confirmation_required'


class IdempotencyConflict(ValueError):
    """同一稳定标识被绑定到不同权限语义。"""


def required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')


def utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{name} must be an aware UTC datetime')


@dataclass(frozen=True)
class ActorEvidence:
    actor_id: str
    role: Role
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        required(self.actor_id, 'actor_id')
        if type(self.sources) is not tuple or type(self.role) is not Role or not self.sources:
            raise ValueError('actor evidence requires a role and sources')
        sources = tuple(source for source in self.sources)
        for source in sources:
            required(source, 'source')
        object.__setattr__(self, 'sources', sources)


@dataclass(frozen=True)
class AuthorizationRequest:
    request_id: str
    actor: ActorEvidence
    session_id: str
    task_id: str | None
    intent: str
    capability: str
    object_scope: str
    risk_scope: str
    within_intent_scope: bool
    reversibility: Reversibility
    provenance_summary: str
    now: datetime
    action_fingerprint: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, 'request_id'), (self.session_id, 'session_id'),
            (self.intent, 'intent'), (self.capability, 'capability'),
            (self.object_scope, 'object_scope'), (self.risk_scope, 'risk_scope'),
            (self.provenance_summary, 'provenance_summary'),
        ):
            required(value, name)
        if self.task_id is not None:
            required(self.task_id, 'task_id')
        if type(self.within_intent_scope) is not bool:
            raise ValueError('within_intent_scope must be bool')
        if not isinstance(self.reversibility, Reversibility):
            raise ValueError('reversibility must be a Reversibility')
        utc(self.now, 'now')
        if not isinstance(self.action_fingerprint, str) or re.fullmatch(r'[0-9a-f]{64}', self.action_fingerprint) is None:
            raise ValueError('action_fingerprint must be a lowercase 64-hex string')


@dataclass(frozen=True)
class PermissionDecision:
    outcome: PermissionOutcome
    reason: str
    todo_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.outcome) is not PermissionOutcome:
            raise ValueError('outcome must be a PermissionOutcome')
        required(self.reason, 'reason')
        if self.outcome in (PermissionOutcome.ALLOW, PermissionOutcome.DENY):
            if self.todo_id is not None:
                raise ValueError('allow and deny decisions cannot include a todo_id')
        elif self.todo_id is None:
            raise ValueError('confirmation decisions require a todo_id')
        else:
            required(self.todo_id, 'todo_id')


@dataclass(frozen=True)
class PermissionEvaluation:
    """Authorizer-internal result before confirmation persistence."""

    outcome: PermissionOutcome
    reason: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not PermissionOutcome:
            raise ValueError('outcome must be a PermissionOutcome')
        required(self.reason, 'reason')
