"""精确任务授权与长期规则。"""

from dataclasses import dataclass
from datetime import datetime

from .contracts import AuthorizationRequest, required, utc
from .roles import Role


@dataclass(frozen=True)
class Grant:
    grant_id: str
    role: Role
    capability: str
    object_scope: str
    risk_scope: str
    expires_at: datetime
    actor_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    intent: str | None = None
    task_scoped: bool = False

    def __post_init__(self) -> None:
        for value, name in (
            (self.grant_id, 'grant_id'), (self.capability, 'capability'),
            (self.object_scope, 'object_scope'), (self.risk_scope, 'risk_scope'),
        ):
            required(value, name)
        if type(self.role) is not Role or type(self.task_scoped) is not bool:
            raise ValueError('grant role and task_scoped are invalid')
        utc(self.expires_at, 'expires_at')
        for value, name in (
            (self.actor_id, 'actor_id'), (self.task_id, 'task_id'),
            (self.session_id, 'session_id'), (self.intent, 'intent'),
        ):
            if value is not None:
                required(value, name)
        if self.task_scoped and None in (self.actor_id, self.task_id, self.session_id, self.intent):
            raise ValueError('task grant must bind actor, task, session and intent')

    @classmethod
    def for_task(cls, request: AuthorizationRequest, expires_at: datetime) -> 'Grant':
        return cls(
            f'task:{request.request_id}', request.actor.role, request.capability,
            request.object_scope, request.risk_scope, expires_at, request.actor.actor_id,
            request.task_id, request.session_id, request.intent, True,
        )

    @classmethod
    def long_lived(
        cls, grant_id: str, role: Role, capability: str, object_scope: str,
        risk_scope: str, expires_at: datetime, actor_id: str | None = None,
    ) -> 'Grant':
        return cls(grant_id, role, capability, object_scope, risk_scope, expires_at, actor_id=actor_id)

    def matches(self, request: AuthorizationRequest) -> bool:
        if request.now >= self.expires_at:
            return False
        if (
            self.role != request.actor.role
            or self.capability != request.capability
            or self.object_scope != request.object_scope
            or self.risk_scope != request.risk_scope
            or (self.actor_id is not None and self.actor_id != request.actor.actor_id)
        ):
            return False
        if not self.task_scoped:
            return True
        return (
            self.task_id == request.task_id
            and self.session_id == request.session_id
            and self.intent == request.intent
        )
