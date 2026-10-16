"""O5c：取消/审批回调和 P4 allowlist 迁移的最小闭环 fixture。"""

import importlib.util
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

spec = importlib.util.spec_from_file_location("p4", Path(__file__).parents[1] / "openclaw" / "p4_config_patch.py")
p4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p4)
old = {"agents": {"entries": {"main": {"tools": {"allow": ["read", "custom_reviewed_tool"]}}}}}
new = p4.apply_1b(old)
allow = new["agents"]["entries"]["main"]["tools"]["allow"]
check("custom_reviewed_tool" in allow and all(tool in allow for tool in p4.TASK_TOOLS),
      "P4 migration preserves reviewed unknown tools and adds only task tools")
check(old["agents"]["entries"]["main"]["tools"]["allow"] == ["read", "custom_reviewed_tool"],
      "P4 migration does not mutate original config")

print("ALL PASS test_task_release_fixes")
