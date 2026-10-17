"""任务事件流投影的稳定读取模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import ArtifactRef, TaskSpec

TASK_STATUSES = frozenset({
    'accepted', 'scheduled', 'running', 'pausing', 'waiting_permission',
    'waiting_dependency', 'verifying', 'succeeded', 'failed',
    'cancel_requested', 'cancelled', 'interrupted', 'unverifiable',
})


@dataclass(frozen=True)
class TaskProjection:
    """当前任务修订与状态；历史事件仍保留在事件日志中。"""

    task_id: str
    revision: int
    goal: str
    status: str
    priority: int
    parent_task_id: str | None
    created_at: datetime
    updated_at: datetime
    artifacts: tuple[ArtifactRef, ...] = ()
    spec: TaskSpec | None = None

    def __post_init__(self) -> None:
        if self.status not in TASK_STATUSES:
            raise ValueError(f'unsupported task status: {self.status}')
        if self.spec is not None and (
            self.spec.task_id != self.task_id or self.spec.revision != self.revision
        ):
            raise ValueError('projection spec must match task identity and revision')
