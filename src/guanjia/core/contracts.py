"""任务核心跨边界使用的不可变领域 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence


def _identifier(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f'{field_name} must be 1..200 characters without control characters')
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{field_name} must be an aware UTC datetime')
    return value.astimezone(timezone.utc)


def _freeze(value: object) -> object:
    """递归复制且冻结 DTO 的结构化内容。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('DTO float values must be finite')
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError('DTO mapping keys must be strings')
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise ValueError('DTO details must contain JSON-compatible values')


def _frozen_mapping(value: Mapping[str, object] | None = None) -> Mapping[str, object]:
    frozen = _freeze(value or {})
    if not isinstance(frozen, Mapping):
        raise ValueError('DTO details must be mappings')
    return frozen


def _json_copy(value: object) -> object:
    """将冻结的 DTO 值还原为可由标准 JSON 编码器处理的副本。"""
    if isinstance(value, Mapping):
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_copy(item) for item in value]
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ArtifactRef:
    """可交付成果或媒体的稳定引用，不承载原始内容。"""

    uri: str
    kind: str
    sha256: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.uri, 'uri')
        _identifier(self.kind, 'kind')
        if self.sha256 is not None and (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(char not in '0123456789abcdef' for char in self.sha256.lower())
        ):
            raise ValueError('sha256 must be a 64-character hexadecimal digest')
        if self.media_type is not None:
            _identifier(self.media_type, 'media_type')


@dataclass(frozen=True)
class Evidence:
    """任务结论的可追溯证据。"""

    kind: str
    summary: str
    observed_at: datetime = field(default_factory=_utc_now)
    artifacts: tuple[ArtifactRef, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)
    evidence_id: str | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.kind, 'kind')
        _identifier(self.summary, 'summary')
        object.__setattr__(self, 'observed_at', _utc(self.observed_at, 'observed_at'))
        object.__setattr__(self, 'artifacts', tuple(self.artifacts))
        object.__setattr__(self, 'details', _frozen_mapping(self.details))
        if not all(isinstance(artifact, ArtifactRef) for artifact in self.artifacts):
            raise ValueError('evidence artifacts must be ArtifactRef values')
        content = json.dumps(
            {
                'kind': self.kind, 'summary': self.summary,
                'artifacts': [artifact.__dict__ for artifact in self.artifacts],
                'details': _json_copy(self.details),
            },
            sort_keys=True, separators=(',', ':'), allow_nan=False,
        )
        content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if self.evidence_id is not None:
            _identifier(self.evidence_id, 'evidence_id')
        object.__setattr__(self, 'evidence_id', self.evidence_id or content_hash)
        object.__setattr__(self, 'content_hash', content_hash)


@dataclass(frozen=True)
class StructuredError:
    """供主程序作确定性分流的结构化失败。"""

    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.code, 'code')
        _identifier(self.message, 'message')
        if type(self.retryable) is not bool:
            raise ValueError('retryable must be boolean')
        object.__setattr__(self, 'details', _frozen_mapping(self.details))


@dataclass(frozen=True)
class PermissionScope:
    """按任务意图而非单次工具调用定义的授权边界。"""

    purpose: str
    target_scope: str = ''
    risk_scope: str = ''
    grants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.purpose, 'purpose')
        if not isinstance(self.target_scope, str) or not isinstance(self.risk_scope, str):
            raise ValueError('permission scopes must be strings')
        object.__setattr__(self, 'grants', tuple(_identifier(item, 'grant') for item in self.grants))


@dataclass(frozen=True)
class DeliveryRequirements:
    """结果交付约束；真正的输出策略由输出模块实现。"""

    channels: tuple[str, ...] = ()
    must_be_visible: bool = False
    allow_delay: bool = True
    format_hint: str | None = None

    def __post_init__(self) -> None:
        if type(self.must_be_visible) is not bool or type(self.allow_delay) is not bool:
            raise ValueError('delivery flags must be boolean')
        if self.format_hint is not None:
            _identifier(self.format_hint, 'format_hint')
        object.__setattr__(self, 'channels', tuple(_identifier(item, 'channel') for item in self.channels))


@dataclass(frozen=True)
class ResourceRequest:
    """调度器可判定的资源租约请求。"""

    resource: str
    mode: str = 'shared'
    units: int = 1
    release_on_pause: bool = False

    def __post_init__(self) -> None:
        _identifier(self.resource, 'resource')
        if self.mode not in {'shared', 'exclusive'}:
            raise ValueError('resource mode must be shared or exclusive')
        if type(self.units) is not int or self.units < 1:
            raise ValueError('resource units must be a positive integer')
        if type(self.release_on_pause) is not bool:
            raise ValueError('release_on_pause must be boolean')


@dataclass(frozen=True)
class TaskSpec:
    """主程序发给工作程序的版本化工作单。"""

    task_id: str
    revision: int
    goal: str
    constraints: tuple[str, ...] = ()
    references: tuple[ArtifactRef, ...] = ()
    permission_scope: PermissionScope | None = None
    priority: int = 0
    success_criteria: tuple[str, ...] = ()
    delivery_requirements: DeliveryRequirements = field(default_factory=DeliveryRequirements)
    resources: tuple[ResourceRequest, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.task_id, 'task_id')
        _identifier(self.goal, 'goal')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('revision must be a positive integer')
        if type(self.priority) is not int:
            raise ValueError('priority must be an integer')
        object.__setattr__(self, 'constraints', tuple(_identifier(item, 'constraint') for item in self.constraints))
        object.__setattr__(self, 'references', tuple(self.references))
        if not all(isinstance(item, ArtifactRef) for item in self.references):
            raise ValueError('references must contain ArtifactRef values')
        if self.permission_scope is not None and not isinstance(self.permission_scope, PermissionScope):
            raise ValueError('permission_scope must be PermissionScope or None')
        if not isinstance(self.delivery_requirements, DeliveryRequirements):
            raise ValueError('delivery_requirements must be DeliveryRequirements')
        object.__setattr__(
            self, 'success_criteria', tuple(_identifier(item, 'success criterion') for item in self.success_criteria)
        )
        object.__setattr__(self, 'resources', tuple(self.resources))
        if not all(isinstance(item, ResourceRequest) for item in self.resources):
            raise ValueError('resources must contain ResourceRequest values')
        if len({item.resource for item in self.resources}) != len(self.resources):
            raise ValueError('a task may request each resource only once')


@dataclass(frozen=True)
class TaskResult:
    """工作程序针对某个任务修订提交的候选结果。"""

    result_id: str
    task_id: str
    revision: int
    summary: str
    artifacts: tuple[ArtifactRef, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    error: StructuredError | None = None
    key_steps: tuple[str, ...] = ()
    evidence_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        _identifier(self.result_id, 'result_id')
        _identifier(self.task_id, 'task_id')
        _identifier(self.summary, 'summary')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('revision must be a positive integer')
        object.__setattr__(self, 'evidence_at', _utc(self.evidence_at, 'evidence_at'))
        object.__setattr__(self, 'artifacts', tuple(self.artifacts))
        object.__setattr__(self, 'evidence', tuple(self.evidence))
        object.__setattr__(self, 'key_steps', tuple(_identifier(item, 'key step') for item in self.key_steps))


@dataclass(frozen=True)
class WorkerEvent:
    """工作程序的原始事件，不等同于用户可见进度。"""

    task_id: str
    revision: int
    event_type: str
    stage: str
    emitted_at: datetime = field(default_factory=_utc_now)
    percent: int | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    worker_event_id: str | None = None
    sequence: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.task_id, 'task_id')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('revision must be a positive integer')
        if self.event_type not in {
            'accepted', 'progress', 'needs_information', 'needs_permission',
            'subtask_requested', 'artifact', 'completed', 'failed',
            'revision_confirmed', 'health',
        }:
            raise ValueError('unsupported worker event type')
        _identifier(self.stage, 'stage')
        object.__setattr__(self, 'emitted_at', _utc(self.emitted_at, 'emitted_at'))
        if self.percent is not None and (
            type(self.percent) is not int or not 0 <= self.percent <= 100
        ):
            raise ValueError('percent must be an integer between 0 and 100')
        object.__setattr__(self, 'payload', _frozen_mapping(self.payload))
        if self.worker_event_id is not None:
            _identifier(self.worker_event_id, 'worker_event_id')
        if self.sequence is not None and (type(self.sequence) is not int or self.sequence < 0):
            raise ValueError('sequence must be a non-negative integer')

    @classmethod
    def progress(
        cls, task_id: str, revision: int, stage: str, percent: int | None = None,
        *, worker_event_id: str | None = None, sequence: int | None = None,
    ) -> 'WorkerEvent':
        return cls(
            task_id, revision, 'progress', stage, percent=percent,
            worker_event_id=worker_event_id, sequence=sequence,
        )

    @classmethod
    def completed(
        cls, task_id: str, revision: int, summary: str, *,
        worker_event_id: str | None = None, sequence: int | None = None,
    ) -> 'WorkerEvent':
        return cls(
            task_id, revision, 'completed', summary, percent=100,
            worker_event_id=worker_event_id, sequence=sequence,
        )


def task_spec_to_dict(spec: TaskSpec) -> dict[str, object]:
    """完整、稳定地持久化 TaskSpec 的每一个字段。"""
    return {
        'task_id': spec.task_id, 'revision': spec.revision, 'goal': spec.goal,
        'constraints': list(spec.constraints),
        'references': [artifact.__dict__.copy() for artifact in spec.references],
        'permission_scope': None if spec.permission_scope is None else {
            'purpose': spec.permission_scope.purpose,
            'target_scope': spec.permission_scope.target_scope,
            'risk_scope': spec.permission_scope.risk_scope,
            'grants': list(spec.permission_scope.grants),
        },
        'priority': spec.priority, 'success_criteria': list(spec.success_criteria),
        'delivery_requirements': {
            'channels': list(spec.delivery_requirements.channels),
            'must_be_visible': spec.delivery_requirements.must_be_visible,
            'allow_delay': spec.delivery_requirements.allow_delay,
            'format_hint': spec.delivery_requirements.format_hint,
        },
        'resources': [
            {'resource': request.resource, 'mode': request.mode, 'units': request.units,
             'release_on_pause': request.release_on_pause}
            for request in spec.resources
        ],
    }


def task_spec_from_dict(value: Mapping[str, object]) -> TaskSpec:
    """从完整持久化表示恢复 TaskSpec。"""
    permission = value['permission_scope']
    return TaskSpec(
        task_id=value['task_id'], revision=value['revision'], goal=value['goal'],
        constraints=tuple(value['constraints']),
        references=tuple(ArtifactRef(**item) for item in value['references']),
        permission_scope=None if permission is None else PermissionScope(**permission),
        priority=value['priority'], success_criteria=tuple(value['success_criteria']),
        delivery_requirements=DeliveryRequirements(**value['delivery_requirements']),
        resources=tuple(ResourceRequest(**item) for item in value['resources']),
    )


class WorkerAdapter(Protocol):
    """Worker 后端协议：session_for + call 两方法（TS agent-runtime / 历史 OpenClaw 同形）。"""

    def session_for(self, logical_session: str) -> str: ...

    def call(
        self, *, logical_session: str, model: str, command: str, operation_id: str | None = None,
        messages: Sequence[Mapping[str, object]],
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class Materials:
    """只有显式 TaskSpec 引用才允许到达 worker。"""

    references: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if type(self.references) is not tuple or not all(isinstance(item, ArtifactRef) for item in self.references):
            raise ValueError('materials references must be a tuple')


@dataclass(frozen=True)
class MemoryPacket:
    """有界的短记忆摘要包（≤8 条 × ≤500 字，Worker 上下文预算）。"""

    summaries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.summaries) is not tuple or len(self.summaries) > 8:
            raise ValueError('memory packet must contain at most eight summaries')
        if not all(type(item) is str and 0 < len(item) <= 500 for item in self.summaries):
            raise ValueError('memory summaries must be short strings')
