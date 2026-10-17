"""结构化权限决策服务。"""

from typing import Iterable

from .confirmations import ConfirmationTodo
from .contracts import AuthorizationRequest, PermissionEvaluation, PermissionOutcome, Reversibility
from .grants import Grant
from .policy import CriticalActionPolicy
from .roles import RolePolicy


class Authorizer:
    def __init__(self, role_policy: RolePolicy, critical_policy: CriticalActionPolicy | None = None) -> None:
        self._role_policy = role_policy
        self._critical_policy = critical_policy or CriticalActionPolicy()

    def authorize(
        self,
        request: AuthorizationRequest,
        grants: Iterable[Grant] = (),
        todos: Iterable[ConfirmationTodo] = (),
    ) -> PermissionEvaluation:
        if not self._role_policy.allows_capability(request.actor.role, request.capability):
            return PermissionEvaluation(PermissionOutcome.DENY, 'role policy does not allow capability')
        if self._critical_policy.requires_confirmation(request):
            if any(todo.matches(request) for todo in todos):
                return PermissionEvaluation(PermissionOutcome.ALLOW, 'matching confirmation todo approved')
            return PermissionEvaluation(PermissionOutcome.CONFIRMATION_REQUIRED, 'critical action requires confirmation')
        if any(grant.matches(request) for grant in grants):
            return PermissionEvaluation(PermissionOutcome.ALLOW, 'matching precise grant')
        if not request.within_intent_scope:
            if any(todo.matches(request) for todo in todos):
                return PermissionEvaluation(PermissionOutcome.ALLOW, 'matching confirmation todo approved')
            outcome = PermissionOutcome.CONFIRMATION_REQUIRED if self._role_policy.scope_outside_requires_confirmation else PermissionOutcome.DENY
            if outcome is PermissionOutcome.CONFIRMATION_REQUIRED:
                return PermissionEvaluation(PermissionOutcome.CONFIRMATION_REQUIRED, 'action is outside authorized intent scope')
            return PermissionEvaluation(outcome, 'action is outside authorized intent scope')
        if (
            request.reversibility is Reversibility.IRREVERSIBLE
            and not self._role_policy.allows_irreversible_action(request.actor.role)
        ):
            if any(todo.matches(request) for todo in todos):
                return PermissionEvaluation(PermissionOutcome.ALLOW, 'matching confirmation todo approved')
            return PermissionEvaluation(PermissionOutcome.CONFIRMATION_REQUIRED, 'role policy requires confirmation for irreversible action')
        return PermissionEvaluation(PermissionOutcome.ALLOW, 'role policy allows in-scope action')
