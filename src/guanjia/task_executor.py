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
        self._verify_tried: set[str] = set()  # L1 验收已重试过的任务（跨重试持久，防无限循环）
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
            verify_note = ""
            if task.get("verify"):
                verify_note = (
                    "\n验收要求：任务完成前必须让真实世界满足以下条件，"
                    f"{task['verify']}。最后一行照常写 TASK_OUTCOME。")
            reply = self.brain_factory(session.session_key).ask(
                retry +
                "你是独立任务 worker。只处理下面这一件任务，不读取或复述主线聊天记录。\n"
                f"任务简述：{task['summary']}\n任务目标：{task['goal']}{verify_note}\n"
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
            # worker 偶尔写中文"已完成"（10-27 实测）：两种写法都认
            done_prefix = "TASK_OUTCOME: completed - " if outcome.startswith("TASK_OUTCOME: completed - ") \
                else ("TASK_OUTCOME: 已完成 - " if outcome.startswith("TASK_OUTCOME: 已完成 - ") else None)
            if done_prefix:
                # L1 验收（方案 D）：有 verify 的任务，completed 声明先过后置条件；
                # 不过 → 带证据重试一次；再不过 → 任务失败收场（证据留档）。
                verify = current.get("verify")
                if verify:
                    from guanjia.task_verify import run_verify
                    ok, evidence = run_verify(verify)
                    if not ok:
                        if session.task_id not in self._verify_tried:
                            with self._lock:
                                self._verify_tried.add(session.task_id)
                            self.tasks.task_report(
                                session.task_id,
                                f"验收未过：{evidence}。带证据重试一次，完成后必须重新满足验收条件。", 50)
                            session2 = WorkerSession(task_id=task["id"], session_key=task["session_key"])
                            with self._lock:
                                self._cancel[task["id"]] = threading.Event()
                            self._pool.submit(self._run, session2, True)
                            return
                        failed = self.tasks.manager.fail(
                            session.task_id, f"验收未过：{evidence}")
                        self._notice("task_failed", failed)
                        return
                done = self.tasks.manager.complete(session.task_id, outcome[len(done_prefix):].strip())
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
