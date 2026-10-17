"""授权决定与确认待办的协调服务。"""

from datetime import timedelta
from typing import Callable, Iterable

from .authorizer import Authorizer
from .confirmations import ConfirmationStatus, ConfirmationTodo, PermissionStore
from .contracts import AuthorizationRequest, IdempotencyConflict, PermissionDecision, PermissionOutcome
from .grants import Grant


class AuthorizationService:
    def __init__(
        self, authorizer: Authorizer, store: PermissionStore,
        todo_id_factory: Callable[[AuthorizationRequest], str] | None = None,
        ttl: timedelta = timedelta(minutes=3),
    ) -> None:
        self._authorizer = authorizer
        self._store = store
        self._todo_id_factory = todo_id_factory or (lambda request: f'confirmation:{request.request_id}')
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError('ttl must be positive')
        self._ttl = ttl

    def decide(self, request: AuthorizationRequest, grants: Iterable[Grant] = ()) -> PermissionDecision:
        existing = self._store.todo_for_request(request.request_id)
        if existing is not None:
            return self._decision_for_existing(existing, request, grants)

        evaluation = self._authorizer.authorize(request, grants, self._store.decisions_for(request))
        if evaluation.outcome is not PermissionOutcome.CONFIRMATION_REQUIRED:
            return PermissionDecision(evaluation.outcome, evaluation.reason)
        todo_id = self._todo_id_factory(request)
        if not isinstance(todo_id, str) or not todo_id.strip():
            raise ValueError('todo_id_factory must return a non-empty string')
        todo = ConfirmationTodo.from_request(
            todo_id, request, request.now + self._ttl
        )
        try:
            self._store.save_todo(todo)
        except IdempotencyConflict:
            winner = self._store.todo_for_request(request.request_id)
            if winner is None:
                raise
            return self._decision_for_existing(winner, request, grants)
        return PermissionDecision(evaluation.outcome, evaluation.reason, todo.todo_id)

    def _decision_for_existing(
        self, existing: ConfirmationTodo, request: AuthorizationRequest, grants: Iterable[Grant],
    ) -> PermissionDecision:
        if not existing.same_request(request):
            raise IdempotencyConflict('request id already belongs to different permission semantics')
        if existing.status is ConfirmationStatus.APPROVED and request.now < existing.expires_at:
            evaluation = self._authorizer.authorize(request, grants, (existing,))
            return PermissionDecision(evaluation.outcome, evaluation.reason)
        if (
            existing.status in (
                ConfirmationStatus.PENDING, ConfirmationStatus.DEFERRED, ConfirmationStatus.UNCERTAIN,
            )
            and request.now < existing.expires_at
        ):
            return PermissionDecision(
                PermissionOutcome.CONFIRMATION_REQUIRED, 'existing confirmation pending', existing.todo_id,
            )
        raise ValueError('terminal or expired confirmation requires a new request_id')


ConfirmationCoordinator = AuthorizationService
