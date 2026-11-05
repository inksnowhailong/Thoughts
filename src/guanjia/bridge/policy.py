"""bridge 的策略引擎：把 bridge HTTP 请求映射到 permission/ 的授权决定。

组合方式（与 guard 插件 levels.json 的语义对齐）：
- L3 → 直接拒绝；
- 角色矩阵（RolePolicy）：owner 只放行 capability_levels.py 登记过的能力，未登记一律 deny；
- L2 → 通过 CriticalActionPolicy（能力集合 = L2 全集）触发 CONFIRMATION_REQUIRED，
  由 AuthorizationService 建立一次性 confirmation todo（TTL=120s，同审批超时）；
- L0/L1 → 在意图范围内放行。

request_id 从 (session, capability, 参数指纹) 确定性生成：审批通过后
用完全相同的参数重查会命中已批准的 todo 而放行——正是语音口头的
「说允许后原样重试一次」语义。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from guanjia.permission import (
    ActorEvidence,
    Authorizer,
    AuthorizationRequest,
    AuthorizationService,
    ConfirmationStatus,
    Decision,
    PermissionOutcome,
    PermissionStore,
    Reversibility,
    Role,
    RolePolicy,
)
from guanjia.permission.policy import CriticalActionPolicy

from .approvals import ApprovalRegistry
from .capability_levels import l2_capabilities, level_for, owner_allowed_capabilities
from .credentials import CONFIRMATION_TIMEOUT_SEC, CredentialLedger

DEFAULT_SESSION_ID = 'voice-main'
OWNER = ActorEvidence(actor_id='guanjia-owner', role=Role.OWNER, sources=('agent-runtime',))
CONFIRMATION_TTL = timedelta(seconds=CONFIRMATION_TIMEOUT_SEC)


def canonical_args(args: object) -> str:
    """参数规范化 JSON：指纹与 request_id 都基于它，重试必须逐字节一致。"""
    return json.dumps(args if isinstance(args, dict) else {}, sort_keys=True,
                      separators=(',', ':'), ensure_ascii=False)


def action_fingerprint(args: object) -> str:
    return hashlib.sha256(canonical_args(args).encode('utf-8')).hexdigest()


def args_summary(args: object, limit: int = 120) -> str:
    text = canonical_args(args)
    return text if len(text) <= limit else f'{text[:limit]}…'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BridgePolicy:
    def __init__(self, store: PermissionStore, approvals: ApprovalRegistry,
                 ledger: CredentialLedger, ttl: timedelta = CONFIRMATION_TTL,
                 now_fn: Callable[[], datetime] = _utc_now) -> None:
        self._store = store
        self._approvals = approvals
        self._ledger = ledger
        self._now = now_fn
        # todo_id → (AuthorizationRequest, PermissionDecision, now)：给 /internal/audit 做
        # permission/audit.py 的结构化落库（PermissionAudit.record 幂等，重复记不冲突）。
        self._confirmation_requests: dict[str, tuple[AuthorizationRequest, object, datetime]] = {}
        role_policy = RolePolicy(
            allowed_capabilities={Role.OWNER: owner_allowed_capabilities()},
            allow_irreversible={Role.OWNER: False},
        )
        critical = CriticalActionPolicy(capabilities=l2_capabilities())
        self._authorizer = Authorizer(role_policy, critical)
        self._service = AuthorizationService(self._authorizer, store, ttl=ttl)

    def confirmation_request(self, todo_id: str):
        """取该 todo 的授权上下文（request, decision, now）；不存在返回 None。"""
        return self._confirmation_requests.get(todo_id)

    # ---------- 权限检查 ----------

    def check(self, session_id: str, capability_id: str, args: object,
              intent: str | None = None, task_id: str | None = None,
              credential: str | None = None) -> dict:
        """{outcome, reason, todo_id?, summary?}；credential 只看不烧。"""
        now = self._now()
        if credential:
            record = self._ledger.peek(credential, now)
            if record is not None and record.capability_id == capability_id:
                return {'outcome': 'allow', 'reason': 'one-time credential approved'}
        level = level_for(capability_id)
        if level == 'L3':
            return {'outcome': 'deny', 'reason': 'capability level L3 is always denied'}
        fingerprint = action_fingerprint(args)
        request = AuthorizationRequest(
            request_id=f'bridge:{session_id}:{capability_id}:{fingerprint}',
            actor=OWNER,
            session_id=session_id,
            task_id=task_id,
            intent=(intent or 'voice-main'),
            capability=capability_id,
            object_scope='home-pc',
            risk_scope=f'level:{level}',
            within_intent_scope=True,
            reversibility=Reversibility.REVERSIBLE,
            provenance_summary='agent-runtime policy check',
            now=now,
            action_fingerprint=fingerprint,
        )
        try:
            decision = self._service.decide(request)
        except ValueError:
            todo = self._store.todo_for_request(request.request_id)
            if todo is not None and todo.status is ConfirmationStatus.REJECTED:
                return {'outcome': 'deny', 'reason': 'rejected by owner'}
            return {'outcome': 'deny', 'reason': 'approval window expired'}
        if decision.outcome is PermissionOutcome.CONFIRMATION_REQUIRED:
            assert decision.todo_id
            summary = f'{capability_id}：{args_summary(args)}'
            self._confirmation_requests[decision.todo_id] = (request, decision, now)
            self._approvals.register(
                decision.todo_id, 'confirmation', summary, session_id,
                capability_id, now, task_id=task_id,
            )
            return {'outcome': decision.outcome.value, 'reason': decision.reason,
                    'todo_id': decision.todo_id, 'summary': summary}
        return {'outcome': decision.outcome.value, 'reason': decision.reason}

    # ---------- 审批决定 ----------

    def confirm(self, todo_id: str, decision: str) -> dict:
        """{credential}（approve）或 {denied: true}（reject）；一次性，过期 120s。

        返回错误时抛 ValueError，由 HTTP 层映射状态码：
        - unknown：todo 不存在；
        - already_used：已经批准/拒绝过；
        - expired：超过 CONFIRMATION_TIMEOUT_SEC。
        """
        now = self._now()
        todo = self._store.todo(todo_id)
        if todo is None:
            raise ValueError('unknown')
        if now >= todo.expires_at:
            raise ValueError('expired')
        if todo.status is not ConfirmationStatus.PENDING:
            raise ValueError('already_used')
        mapping = {'approve': Decision.APPROVE, 'reject': Decision.REJECT}
        if decision not in mapping:
            raise ValueError('bad_decision')
        resolved = self._store.resolve(todo_id, OWNER, mapping[decision], now)
        self._approvals.decide(todo_id, decision, now)
        if decision == 'reject':
            return {'denied': True}
        credential = self._ledger.issue(
            todo_id, todo.session_id, todo.capability, todo.action_fingerprint, now,
        )
        return {'credential': credential}

    def resolve_for_ipc(self, todo_id: str, decision: str) -> None:
        """语音 /decide 路径：只解决 todo 并签发一次性凭证（不返回给语音侧）。

        与 confirm 同样的一次性语义：非 PENDING 状态、已过期均不再受理。
        """
        now = self._now()
        todo = self._store.todo(todo_id)
        if todo is None:
            raise ValueError('unknown')
        if now >= todo.expires_at:
            raise ValueError('expired')
        if todo.status is not ConfirmationStatus.PENDING:
            raise ValueError('already_used')
        mapping = {'approve': Decision.APPROVE, 'reject': Decision.REJECT}
        if decision not in mapping:
            raise ValueError('bad_decision')
        self._store.resolve(todo_id, OWNER, mapping[decision], now)
        self._approvals.decide(todo_id, decision, now)
        if decision == 'approve':
            self._ledger.issue(
                todo_id, todo.session_id, todo.capability, todo.action_fingerprint, now,
            )

    def credential_for(self, todo_id: str, capability_id: str | None = None) -> str:
        """拿指定 todo 名下未用过的凭证（worker 审批回调前签发）。

        permission store 里有对应 todo（走 policy/check 建的）就绑定 todo 的能力与指纹；
        否则（TS 侧自带编号的 worker 审批）绑定调用方登记的 capability。
        """
        now = self._now()
        todo = self._store.todo(todo_id)
        if todo is not None:
            return self._ledger.issue(
                todo_id, todo.session_id, todo.capability, todo.action_fingerprint, now,
            )
        return self._ledger.issue(todo_id, 'worker', capability_id or 'worker_approval',
                                  f'worker:{todo_id}', now)

    def consume_credential(self, credential: str) -> dict | None:
        """一次性消费凭证；无效返回 None。"""
        return self._ledger.consume(credential, self._now())
