"""O5 独立任务 worker：显式领取、独立会话、协作式取消；不杀进程。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from guanjia.announcement import AnnouncementPolicy
from guanjia.brain import Brain
from guanjia.task_manager import RUNNING, WAITING_APPROVAL, TaskError
from guanjia.task_service import TaskService, WorkerSession


@dataclass(frozen=True)
class TaskNotice:
    kind: str
    task: dict
    speak: bool


class TaskWorkerExecutor:
    """必须由受控接线显式 submit；task_start 永远不会自动调用本类。"""

    def __init__(
        self,
        tasks: TaskService,
        *,
        brain_factory: Callable[[str], Brain] = lambda session_key: Brain(session_key=session_key),
        policy: AnnouncementPolicy | None = None,
        on_notice: Callable[[TaskNotice], None] | None = None,
    ) -> None:
        self.tasks, self.brain_factory = tasks, brain_factory
        self.policy, self.on_notice = policy or AnnouncementPolicy(lambda: False), on_notice
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="task-worker")
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def submit(self, task_id: str) -> WorkerSession:
        """唯一领取入口：先 reserve，再把网关调用放入 worker 线程。"""
        session = self.tasks.workers.reserve(task_id)
        with self._lock:
            self._cancel[task_id] = threading.Event()
        self._pool.submit(self._run, session)
        return session

    def request_cancel(self, task_id: str, reason: str = "") -> dict:
        """协作式：停止后续步骤；正在阻塞的 Brain.ask 无安全硬中止 API。"""
        task = self.tasks.task_cancel(task_id, reason)
        with self._lock:
            event = self._cancel.get(task_id)
            if event is not None:
                event.set()
        return task

    def resume_after_approval(self, task_id: str) -> None:
        """批准后以原 session 重开一轮；闩锁仍只允许保存参数的一次重试。"""
        task = self.tasks.manager.resume_after_approval(task_id)
        session = WorkerSession(task_id=task["id"], session_key=task["session_key"])
        with self._lock:
            self._cancel[task_id] = threading.Event()
        self._pool.submit(self._run, session, True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, session: WorkerSession, approved_retry: bool = False) -> None:
        cancel = self._cancel[session.task_id]
        try:
            task = self.tasks.task_status(session.task_id)
            self.tasks.task_report(session.task_id, "独立任务会话已开始", 0)
            if cancel.is_set():
                return
            retry = "__guanjia_approved_retry__\n" if approved_retry else ""
            reply = self.brain_factory(session.session_key).ask(
                retry +
                "你是独立任务 worker。只处理下面这一件任务，不读取或复述主线聊天记录。\n"
                f"任务简述：{task['summary']}\n任务目标：{task['goal']}\n"
                "只能使用现有工具；所有工具调用仍受守卫和审批约束。不要下载、执行 shell 或绕过审批，除非用户目标明确需要且守卫已允许。"
                "若工具被拦下需审批，停止后续步骤并如实说明。"
                "只有已从工具得到成功结果时，最后一行写 `TASK_OUTCOME: completed - <简短结果>`；否则写 `TASK_OUTCOME: failed - <原因>`。",
            )
            if cancel.is_set():
                return
            current = self.tasks.task_status(session.task_id)
            if current["status"] != RUNNING:
                return
            outcome = next((line.strip() for line in reversed(reply.splitlines()) if line.strip()), "")
            if outcome.startswith("TASK_OUTCOME: completed - "):
                done = self.tasks.manager.complete(session.task_id, outcome.removeprefix("TASK_OUTCOME: completed - ").strip())
                self._notice("task_completed", done)
            else:
                why = outcome.removeprefix("TASK_OUTCOME: failed - ").strip() if outcome.startswith("TASK_OUTCOME: failed - ") else "任务会话未给出完成证据"
                failed = self.tasks.manager.fail(session.task_id, why or "任务会话失败")
                self._notice("task_failed", failed)
        except Exception as exc:  # noqa: BLE001
            try:
                current = self.tasks.task_status(session.task_id)
                if current["status"] == RUNNING and not cancel.is_set():
                    failed = self.tasks.manager.fail(session.task_id, f"{type(exc).__name__}: {exc}")
                    self._notice("task_failed", failed)
            except TaskError:
                pass
        finally:
            with self._lock:
                self._cancel.pop(session.task_id, None)

    def _notice(self, kind: str, task: dict) -> None:
        decision = self.policy.should_announce(kind, user_initiated=False)
        if self.on_notice is not None:
            self.on_notice(TaskNotice(kind, task, decision.speak))
