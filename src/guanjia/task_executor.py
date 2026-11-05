"""O5 独立任务 worker：显式领取、独立会话、协作式取消；不杀进程。

Worker 后端（Wave1-C 反转，2026-11-05）：
- GUANJIA_WORKER_BACKEND=openclaw（缺省）：Brain(session_key).ask() 走 OpenClaw 网关，行为不变；
- GUANJIA_WORKER_BACKEND=ts：经 core/agent_runtime_adapter.AgentRuntimeAdapter 调
  TS agent-runtime /v1/worker/*（LangGraph Worker，审批 interrupt/恢复在 TS 侧完成），
  拿到 outcome 后仍走本模块的 L1 验收（task_verify）与 complete/fail 落库。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from guanjia.announcement import AnnouncementPolicy
from guanjia.brain import Brain
from guanjia.core.contracts import TaskSpec
from guanjia.task_manager import RUNNING, WAITING_APPROVAL, TaskError
from guanjia.task_service import TaskService, WorkerSession


def _ts_identifier(text: str, limit: int = 200) -> str:
    """自由文本 → core 契约 identifier：控制字符（含换行）转空格，按码点截断。"""
    cleaned = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in str(text or '')).strip()
    return ''.join(list(cleaned)[:limit]) or '-'


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
        worker_adapter: object | None = None,
    ) -> None:
        self.tasks, self.brain_factory = tasks, brain_factory
        self.policy, self.on_notice = policy or AnnouncementPolicy(lambda: False), on_notice
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="task-worker")
        self._cancel: dict[str, threading.Event] = {}
        self._verify_tried: set[str] = set()  # L1 验收已重试过的任务（跨重试持久，防无限循环）
        self._lock = threading.RLock()
        # Worker 后端开关（Wave1-C）：缺省 openclaw 行为不变；ts 用反转后的适配器
        self._backend = os.environ.get("GUANJIA_WORKER_BACKEND", "openclaw").strip().lower()
        if self._backend == "ts":
            if worker_adapter is not None:
                self._adapter = worker_adapter
            else:
                from guanjia.core.agent_runtime_adapter import build_worker_adapter

                self._adapter = build_worker_adapter()
            sink = getattr(self._adapter, "set_event_sink", None)
            if callable(sink):
                sink(self._on_worker_event)
        else:
            self._adapter = None

    # ---- TS 后端（GUANJIA_WORKER_BACKEND=ts）----

    def _on_worker_event(self, event: object) -> None:
        """适配器 SSE 事件 → 任务进度上报（尽力而为，不反压 worker）。"""
        try:
            if getattr(event, "event_type", "") == "progress":
                self.tasks.task_report(
                    event.task_id, str(event.stage or "进行中")[:80],
                    None if event.percent is None else max(0, min(100, int(event.percent))),
                )
        except Exception:  # noqa: BLE001
            pass

    def _task_spec(self, task: dict) -> TaskSpec:
        task_manager_task = task
        return TaskSpec(
            task_id=_ts_identifier(task_manager_task["id"]),
            revision=1,
            goal=_ts_identifier(task_manager_task.get("goal") or task_manager_task.get("summary") or "完成任务"),
        )

    def _run_ts(self, session: WorkerSession) -> None:
        task = self.tasks.task_status(session.task_id)
        self.tasks.task_report(session.task_id, "独立任务会话已开始（TS worker）", 0)
        spec = self._task_spec(task)
        logical = f"task/{spec.task_id}/r{spec.revision}"
        try:
            response = self._adapter.call(
                logical_session=logical, model="worker", command="start",
                operation_id=uuid.uuid4().hex,
                messages=({"role": "user", "content": {
                    "task": _task_spec_dict(spec), "materials": [], "memory_packet": [],
                }},),
            )
        except Exception as exc:  # noqa: BLE001
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if session.task_id in self._cancel and self._cancel[session.task_id].is_set():
            return
        if not response.get("ok"):
            failed = self.tasks.manager.fail(session.task_id, f"TS worker 后端失败：{response.get('error')}")
            self._notice("task_failed", failed)
            return
        status = str(response.get("status") or "failed")
        summary = str(response.get("summary") or "")
        if status == "cancelled":
            return  # 取消已由 request_cancel 落库
        if status == "completed":
            self._finish(session.task_id, f"TASK_OUTCOME: completed - {summary}")
        else:
            self._finish(session.task_id, f"TASK_OUTCOME: failed - {summary or 'worker 未给出结果'}")

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
        if self._adapter is not None:
            # TS 后端：通知 runtime 取消（图在当前工具完成后收敛；挂起审批按拒绝唤醒）
            try:
                self._adapter.call(
                    logical_session=f"task/{task_id}/r1", model="worker",
                    command="cancel", messages=(),
                )
            except Exception:  # noqa: BLE001
                pass
        return task

    def resume_after_approval(self, task_id: str) -> None:
        """批准后以原 session 重开一轮；闩锁仍只允许保存参数的一次重试。

        TS 后端：审批恢复由 bridge 回调 TS /v1/worker/runs/{id}/resume 完成，
        本方法只核对状态，不重开任务（避免与 TS 侧重复执行）。
        """
        task = self.tasks.manager.resume_after_approval(task_id)
        if self._backend == "ts":
            return
        session = WorkerSession(task_id=task["id"], session_key=task["session_key"])
        with self._lock:
            self._cancel[task_id] = threading.Event()
        self._pool.submit(self._run, session, True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, session: WorkerSession, approved_retry: bool = False) -> None:
        try:
            if self._adapter is not None:
                self._run_ts(session)
                return
            cancel = self._cancel[session.task_id]
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
            self._finish(session.task_id, reply, cancel)
        except Exception as exc:  # noqa: BLE001
            try:
                current = self.tasks.task_status(session.task_id)
                if current["status"] == RUNNING and not self._cancel.get(session.task_id, threading.Event()).is_set():
                    failed = self.tasks.manager.fail(session.task_id, f"{type(exc).__name__}: {exc}")
                    self._notice("task_failed", failed)
            except TaskError:
                pass
        finally:
            with self._lock:
                self._cancel.pop(session.task_id, None)

    def _finish(self, task_id: str, reply: str, cancel: threading.Event | None = None) -> None:
        """两条后端共用的收尾：解析 TASK_OUTCOME → L1 验收 → complete/fail 落库。"""
        current = self.tasks.task_status(task_id)
        if current["status"] != RUNNING:
            return
        if cancel is not None and cancel.is_set():
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
                    if task_id not in self._verify_tried:
                        with self._lock:
                            self._verify_tried.add(task_id)
                        self.tasks.task_report(
                            task_id,
                            f"验收未过：{evidence}。带证据重试一次，完成后必须重新满足验收条件。", 50)
                        if self._adapter is not None:
                            # TS 后端：重新提交一次完整 worker 运行（新 run）
                            self._pool.submit(self._run, WorkerSession(task_id=task_id, session_key=current["session_key"]))
                        else:
                            session2 = WorkerSession(task_id=current["id"], session_key=current["session_key"])
                            with self._lock:
                                self._cancel[current["id"]] = threading.Event()
                            self._pool.submit(self._run, session2, True)
                        return
                    failed = self.tasks.manager.fail(task_id, f"验收未过：{evidence}")
                    self._notice("task_failed", failed)
                    return
            done = self.tasks.manager.complete(task_id, outcome[len(done_prefix):].strip())
            self._notice("task_completed", done)
        else:
            why = outcome.removeprefix("TASK_OUTCOME: failed - ").strip() if outcome.startswith("TASK_OUTCOME: failed - ") else "任务会话未给出完成证据"
            failed = self.tasks.manager.fail(task_id, why or "任务会话失败")
            self._notice("task_failed", failed)

    def _notice(self, kind: str, task: dict) -> None:
        decision = self.policy.should_announce(kind, user_initiated=False)
        if self.on_notice is not None:
            self.on_notice(TaskNotice(kind, task, decision.speak))


def _task_spec_dict(spec: TaskSpec) -> dict[str, object]:
    """TaskSpec → TS WorkerRunRequest.task_spec 线形状（core.contracts.task_spec_to_dict 同形）。"""
    from guanjia.core.contracts import task_spec_to_dict

    return task_spec_to_dict(spec)
