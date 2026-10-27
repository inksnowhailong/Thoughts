"""离线任务管家：只管理状态和持久化，不接入网关或执行器。"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
WAITING_APPROVAL = "waiting_approval"
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})
TASK_STATES = frozenset({QUEUED, RUNNING, WAITING_APPROVAL, INTERRUPTED, *TERMINAL_STATES})
_TRANSITIONS = {
    QUEUED: {RUNNING, CANCELLED},
    RUNNING: {WAITING_APPROVAL, COMPLETED, FAILED, CANCELLED},
    WAITING_APPROVAL: {RUNNING, FAILED, CANCELLED},
    INTERRUPTED: {QUEUED, CANCELLED},
    COMPLETED: set(),
    FAILED: set(),
    CANCELLED: set(),
}


class TaskError(ValueError):
    """任务不存在、状态不对或参数不合法。"""


class ConcurrencyLimitError(TaskError):
    """运行中任务达到并发上限。"""


@dataclass
class Approval:
    """任务关联的待放行事项。"""

    id: str
    description: str
    created_at: float
    expires_at: float | None = None


@dataclass
class Task:
    """可落盘的任务状态。"""

    id: str
    summary: str
    goal: str
    session_key: str = ""
    status: str = QUEUED
    progress: str = ""
    progress_percent: float | None = None
    approval: Approval | None = None
    run_at: float | None = None  # 定时执行时刻（epoch 秒）；None = 立即执行
    verify: dict | None = None   # L1 验收后置条件（方案 D）：过不了不许 complete
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        approval = raw.get("approval")
        return cls(
            id=str(raw["id"]),
            summary=str(raw["summary"]),
            goal=str(raw["goal"]),
            session_key=str(raw.get("session_key", "")),
            status=str(raw["status"]),
            progress=str(raw.get("progress", "")),
            progress_percent=raw.get("progress_percent"),
            approval=Approval(**approval) if approval else None,
            run_at=raw.get("run_at"),
            verify=raw.get("verify") if isinstance(raw.get("verify"), dict) else None,
            result=str(raw.get("result", "")),
            error=str(raw.get("error", "")),
            created_at=float(raw["created_at"]),
            updated_at=float(raw["updated_at"]),
        )


class TaskManager:
    """线程安全的任务状态仓库；调用方负责实际执行和放行。"""

    def __init__(self, journal_path: str | Path, *, max_concurrent: int = 2) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent 必须至少为 1")
        self.journal_path = Path(journal_path)
        self.max_concurrent = max_concurrent
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()

    @classmethod
    def restore(cls, journal_path: str | Path, *, max_concurrent: int = 2) -> TaskManager:
        """从 JSONL 快照恢复；重启前活跃的任务改为 interrupted，绝不自动续跑。"""
        manager = cls(journal_path, max_concurrent=max_concurrent)
        if not manager.journal_path.exists():
            return manager
        with manager.journal_path.open("r", encoding="utf-8") as journal:
            for line in journal:
                try:
                    event = json.loads(line)
                    if event.get("type") != "task_snapshot":
                        continue
                    task = Task.from_dict(event["task"])
                    if task.status in TASK_STATES:
                        manager._tasks[task.id] = task
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    LOGGER.warning("忽略损坏的任务日志行")
        # 旧进程和它启动的工具都已不可控；恢复后必须由接线层明确 retry 才能重新排队。
        for task in manager._tasks.values():
            if task.status in {RUNNING, WAITING_APPROVAL}:
                task.status = INTERRUPTED
                task.error = "管家重启中断，未自动继续"
                task.updated_at = time.time()
                manager._persist(task, "interrupted_on_restore")
        return manager

    def create(self, summary: str, goal: str, run_at: float | None = None,
               verify: dict | None = None) -> dict[str, Any]:
        """新任务先进入队列，由执行器随后调用 start；run_at 在未来时为定时任务。

        verify 是 L1 验收后置条件（方案 D），例如：
        {"kind": "process", "name": "cloudmusic"}     启动类：核实进程在跑
        {"kind": "file", "path": "D:/x", "min_kb": 1} 产出类：核实文件存在
        {"kind": "camera_contains", "text": "沙发"}   摄像头类：画面里有目标
        {"kind": "screen_contains", "text": "结算"}   屏幕类：截屏里有目标
        """
        if not summary.strip() or not goal.strip():
            raise TaskError("任务简述和目标不能为空")
        if run_at is not None and run_at <= time.time():
            run_at = None  # 过去的时间视为立即执行，不报错
        if verify is not None and not isinstance(verify, dict):
            raise TaskError("verify 必须是对象")
        with self._lock:
            task_id = uuid.uuid4().hex
            task = Task(
                id=task_id,
                summary=summary.strip(),
                goal=goal.strip(),
                session_key=f"agent:worker:task-{task_id}",
                run_at=run_at,
                verify=verify,
            )
            self._tasks[task.id] = task
            self._persist(task, "created")
            return self._snapshot(task)

    def get(self, task_id: str) -> dict[str, Any]:
        """返回一个与内部状态隔离的任务快照。"""
        with self._lock:
            return self._snapshot(self._require(task_id))

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """按创建时间返回任务快照，可选按状态筛选。"""
        if status is not None and status not in TASK_STATES:
            raise TaskError(f"未知状态: {status}")
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda task: task.created_at)
            return [self._snapshot(task) for task in tasks if status is None or task.status == status]

    def start(self, task_id: str) -> dict[str, Any]:
        """占用一个并发槽位，并把排队任务标为进行中。"""
        with self._lock:
            task = self._require(task_id)
            if task.status != QUEUED:
                raise TaskError("只有 queued 任务可以开始")
            if self._running_count() >= self.max_concurrent:
                raise ConcurrencyLimitError(f"并发上限为 {self.max_concurrent}")
            return self._transition(task, RUNNING, "started")

    def report_progress(self, task_id: str, progress: str, percent: float | None = None) -> dict[str, Any]:
        """记录执行器上报的简短进度。"""
        if percent is not None and not 0 <= percent <= 100:
            raise TaskError("进度百分比必须在 0 到 100 之间")
        with self._lock:
            task = self._require(task_id)
            if task.status != RUNNING:
                raise TaskError("只有 running 任务可以上报进度")
            task.progress = progress.strip()
            task.progress_percent = percent
            task.updated_at = time.time()
            self._persist(task, "progress_reported")
            return self._snapshot(task)

    def bind_approval(
        self, task_id: str, approval_id: str, description: str, *, expires_at: float | None = None
    ) -> dict[str, Any]:
        """绑定待放行事项，任务随即等待放行。"""
        if not approval_id.strip() or not description.strip():
            raise TaskError("待放行事项编号和描述不能为空")
        with self._lock:
            task = self._require(task_id)
            if task.status != RUNNING:
                raise TaskError("只有 running 任务可以等待放行")
            task.approval = Approval(approval_id.strip(), description.strip(), time.time(), expires_at)
            return self._transition(task, WAITING_APPROVAL, "approval_bound")

    def resume_after_approval(self, task_id: str) -> dict[str, Any]:
        """放行已由外部系统处理后，恢复任务执行。"""
        with self._lock:
            task = self._require(task_id)
            if task.status != WAITING_APPROVAL:
                raise TaskError("只有 waiting_approval 任务可以恢复")
            return self._transition(task, RUNNING, "approval_resumed")

    def retry_interrupted(self, task_id: str) -> dict[str, Any]:
        """由未来接线层明确决定重试；本方法只重新排队，不会恢复旧进程。"""
        with self._lock:
            task = self._require(task_id)
            if task.status != INTERRUPTED:
                raise TaskError("只有 interrupted 任务可以重新排队")
            task.approval = None
            task.error = ""
            return self._transition(task, QUEUED, "retry_queued")

    def complete(self, task_id: str, result: str = "") -> dict[str, Any]:
        """标记任务已完成。"""
        with self._lock:
            task = self._require(task_id)
            task.result = result.strip()
            return self._transition(task, COMPLETED, "completed")

    def fail(self, task_id: str, error: str) -> dict[str, Any]:
        """标记任务失败并保存原因。"""
        if not error.strip():
            raise TaskError("失败原因不能为空")
        with self._lock:
            task = self._require(task_id)
            task.error = error.strip()
            return self._transition(task, FAILED, "failed")

    def cancel(self, task_id: str, reason: str = "") -> dict[str, Any]:
        """只更新状态；不会终止任何进程。"""
        with self._lock:
            task = self._require(task_id)
            if task.status in TERMINAL_STATES:
                raise TaskError("已结束任务不能取消")
            task.result = reason.strip()
            return self._transition(task, CANCELLED, "cancelled")

    def summary(self) -> dict[str, Any]:
        """提供给未来语音或面板的紧凑摘要。"""
        with self._lock:
            counts = {state: 0 for state in TASK_STATES}
            for task in self._tasks.values():
                counts[task.status] += 1
            active = [self._snapshot(task) for task in self._tasks.values() if task.status in {RUNNING, WAITING_APPROVAL}]
            interrupted = [self._snapshot(task) for task in self._tasks.values() if task.status == INTERRUPTED]
            recent = [self._snapshot(task) for task in self._tasks.values()]
            active.sort(key=lambda task: task["updated_at"], reverse=True)
            interrupted.sort(key=lambda task: task["updated_at"], reverse=True)
            recent.sort(key=lambda task: task["updated_at"], reverse=True)
            return {
                "counts": counts,
                "max_concurrent": self.max_concurrent,
                "running_slots": self._running_count(),
                "active_tasks": active,
                "interrupted_tasks": interrupted,
                # 面板只读展示最近状态；不提供领取、取消或任何执行入口。
                "recent_tasks": recent[:8],
            }

    def _require(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskError(f"任务不存在: {task_id}") from exc

    def _running_count(self) -> int:
        return sum(task.status == RUNNING for task in self._tasks.values())

    def _transition(self, task: Task, target: str, event: str) -> dict[str, Any]:
        if target not in _TRANSITIONS[task.status]:
            raise TaskError(f"不允许状态转换: {task.status} -> {target}")
        task.status = target
        task.updated_at = time.time()
        self._persist(task, event)
        return self._snapshot(task)

    def _persist(self, task: Task, event: str) -> None:
        entry = {"type": "task_snapshot", "event": event, "at": time.time(), "task": task.to_dict()}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as journal:
            journal.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            journal.flush()
            os.fsync(journal.fileno())

    @staticmethod
    def _snapshot(task: Task) -> dict[str, Any]:
        return copy.deepcopy(task.to_dict())
