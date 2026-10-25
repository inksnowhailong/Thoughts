"""O3 任务接口与独立 worker 会话适配层；本模块绝不自动调用模型或工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from guanjia.task_manager import QUEUED, RUNNING, TaskError, TaskManager


@dataclass(frozen=True)
class WorkerSession:
    """已预留的独立任务会话；未来受控执行器才能使用它请求模型。"""

    task_id: str
    session_key: str


class TaskWorkerAdapter:
    """把状态仓库映射为独立会话句柄，不派发模型、不执行命令。"""

    def __init__(self, manager: TaskManager) -> None:
        self.manager = manager

    def reserve(self, task_id: str) -> WorkerSession:
        """执行器明确领取任务时才占并发槽位。"""
        task = self.manager.get(task_id)
        if task["status"] != QUEUED:
            raise TaskError("只有 queued 任务可以领取")
        started = self.manager.start(task_id)
        return WorkerSession(task_id=started["id"], session_key=started["session_key"])

    def session(self, task_id: str) -> WorkerSession:
        """仅为 running 任务返回会话键，防止未领取任务被误派发。"""
        task = self.manager.get(task_id)
        if task["status"] != RUNNING:
            raise TaskError("只有 running 任务有 worker 会话")
        if not task["session_key"]:
            raise TaskError("任务缺少 worker 会话键")
        return WorkerSession(task_id=task["id"], session_key=task["session_key"])


class TaskService:
    """给未来 OpenClaw 工具与语音接线使用的受控任务接口。"""

    def __init__(self, manager: TaskManager, workers: TaskWorkerAdapter | None = None) -> None:
        self.manager = manager
        self.workers = workers or TaskWorkerAdapter(manager)

    def task_start(self, summary: str, goal: str, run_at: float | None = None) -> dict[str, Any]:
        """创建 queued 任务并返回独立会话键；不自动执行，主线无需等待。run_at 在未来时为定时任务。"""
        return self.manager.create(summary, goal, run_at=run_at)

    def task_status(self, task_id: str | None = None) -> dict[str, Any]:
        """查看单项或聚合状态；不读取任务会话正文。"""
        return self.manager.get(task_id) if task_id else self.manager.summary()

    def task_cancel(self, task_id: str, reason: str = "") -> dict[str, Any]:
        """记录取消请求；不杀进程，后续执行器必须自行协作停止。"""
        return self.manager.cancel(task_id, reason)

    def task_report(self, task_id: str, progress: str, percent: float | None = None) -> dict[str, Any]:
        """仅 running worker 可以上报进度。"""
        return self.manager.report_progress(task_id, progress, percent)

    def task_waiting_approval(self, task_id: str, approval_id: str, description: str, expires_at: float | None = None) -> dict[str, Any]:
        """守卫拦下 worker 工具时绑定待放行项；不保存原始工具参数。"""
        return self.manager.bind_approval(task_id, approval_id, description, expires_at=expires_at)
