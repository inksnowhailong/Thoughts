"""O5b：只把当前主线 task_start 新建项延后到首句安全播完后领取。"""

from __future__ import annotations

import threading
from collections.abc import Callable

from guanjia.task_manager import ConcurrencyLimitError, TaskError


class TaskDispatchScheduler:
    """不扫描 queued；每个候选只能由本轮 /start 回调登记一次。"""

    def __init__(self, emit: Callable[[str, str, str], None]) -> None:
        self._emit = emit
        self._lock = threading.RLock()
        self._candidates: dict[str, list[str]] = {}

    def record_start(self, turn_id: str | None, task_id: str) -> bool:
        """仅绑定有语音主线记录的刚创建任务。"""
        if not turn_id:
            self._emit("task.dispatch_skipped", task_id, "没有主线语音轮")
            return False
        with self._lock:
            self._candidates.setdefault(turn_id, []).append(task_id)
        self._emit("task.dispatch_waiting", task_id, "等待主线首句播完")
        return True

    def finish_turn(self, turn_id: str | None, *, reply_delivered: bool, submit: Callable[[str], object]) -> list[str]:
        """回复失败、打断或未入播放链即永久放弃本轮候选，任务仍保持 queued。"""
        if not turn_id:
            return []
        with self._lock:
            task_ids = self._candidates.pop(turn_id, [])
        if not reply_delivered:
            for task_id in task_ids:
                self._emit("task.dispatch_skipped", task_id, "主线首句未成功播放")
            return []
        dispatched = []
        for task_id in task_ids:
            try:
                submit(task_id)  # O5 明确 reserve；TaskManager 仍是并发上限的唯一裁决者
            except ConcurrencyLimitError:
                self._emit("task.dispatch_skipped", task_id, "并发槽位已满")
            except TaskError as exc:
                self._emit("task.dispatch_skipped", task_id, str(exc))
            else:
                dispatched.append(task_id)
                self._emit("task.dispatched", task_id, "主线首句已播放，独立 worker 已领取")
        return dispatched
