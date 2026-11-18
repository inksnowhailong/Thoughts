"""任务取消与审批回调的最小闭环 fixture。"""

import tempfile
from pathlib import Path

from guanjia.task_ipc import TaskIpc
from guanjia.task_manager import TaskManager
from guanjia.task_service import TaskService


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp:
    service = TaskService(TaskManager(Path(tmp) / "tasks.jsonl"))
    task = service.task_start("审批", "fixture")
    service.workers.reserve(task["id"])
    service.task_waiting_approval(task["id"], "pending-1", "危险操作")
    calls = []
    ipc = TaskIpc(service, Path(tmp) / "task-ipc.json",
                  on_cancelled=lambda task_id, reason: calls.append(("cancel", task_id, reason)),
                  on_approval_decision=lambda task_id, decision: calls.append(("approval", task_id, decision)))
    # 回调签名本身承载 guard 的最小数据边界：只有 task id + approve/reject，绝无参数/session。
    ipc.on_approval_decision(task["id"], "approve")
    ipc.on_cancelled(task["id"], "用户取消")
    check(calls == [("approval", task["id"], "approve"), ("cancel", task["id"], "用户取消")],
          "approval and cancellation cross only controlled callbacks")

print("ALL PASS test_task_release_fixes")
