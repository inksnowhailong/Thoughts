"""权限领域公共导出。"""

from .agent_gate import AgentGate, HmacSandboxVerifier, SandboxDecision, SandboxProof, SandboxVerifier
from .authorizer import Authorizer
from .confirmations import ConfirmationStatus, ConfirmationTodo, InMemoryPermissionStore, MigrationRequired, PermissionStore, SQLitePermissionStore
from .contracts import ActorEvidence, AuthorizationRequest, Decision, IdempotencyConflict, PermissionDecision, PermissionOutcome, Reversibility
from .grants import Grant
from .policy import CriticalActionPolicy
from .roles import Role, RolePolicy
from .service import AuthorizationService, ConfirmationCoordinator

__all__ = [
    'ActorEvidence', 'AgentGate', 'Authorizer', 'AuthorizationRequest', 'AuthorizationService',
    'ConfirmationStatus', 'ConfirmationTodo', 'CriticalActionPolicy', 'Decision', 'Grant',
    'IdempotencyConflict', 'InMemoryPermissionStore', 'MigrationRequired', 'SQLitePermissionStore', 'PermissionStore', 'PermissionDecision', 'PermissionOutcome', 'Reversibility', 'Role',
    'RolePolicy', 'ConfirmationCoordinator', 'SandboxDecision', 'SandboxProof', 'SandboxVerifier', 'HmacSandboxVerifier',
]
