"""关键动作集合。"""

from dataclasses import dataclass, field

from .contracts import AuthorizationRequest


@dataclass(frozen=True)
class CriticalActionPolicy:
    capabilities: frozenset[str] = field(default_factory=lambda: frozenset({
        'account.modify', 'credential.export', 'data.permanent_delete',
        'finance.transfer', 'message.public_send', 'system.destructive',
    }))
    risk_scopes: frozenset[str] = field(default_factory=lambda: frozenset({
        'credential', 'destructive', 'financial', 'permission-change',
        'public-representation',
    }))

    def requires_confirmation(self, request: AuthorizationRequest) -> bool:
        return request.capability in self.capabilities or request.risk_scope in self.risk_scopes
