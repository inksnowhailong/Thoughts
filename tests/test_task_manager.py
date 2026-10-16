r"""O3 离线任务管家最小验证。

运行：cd /d F:\coding\guanjia && .venv\Scripts\python.exe tests\test_task_manager.py
"""

import tempfile
from pathlib import Path

from guanjia.task_manager import (
    CANCELLED,
    COMPLETED,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    WAITING_APPROVAL,
    ConcurrencyLimitError,
    TaskError,
    TaskManager,
)


def check(condition: bool, message: str) -> None:
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp:
    journal = Path(tmp) / "tasks.jsonl"
    manager = TaskManager(journal)

    first = manager.create("下载地图", "下载离线地图包")
    second = manager.create("整理照片", "按月份归档照片")
    third = manager.create("清理缓存", "清理浏览器缓存")
    check(first["status"] == QUEUED and len(manager.list()) == 3, "create queues tasks")

    manager.start(first["id"])
    manager.start(second["id"])
    try:
        manager.start(third["id"])
    except ConcurrencyLimitError:
        pass
    else:
        check(False, "concurrency limit rejects third running task")
    check(manager.summary()["running_slots"] == 2, "summary exposes running slots")

    updated = manager.report_progress(first["id"], "已下载一半", 50)
    check(updated["progress_percent"] == 50 and updated["status"] == RUNNING, "progress is stored")

    waiting = manager.bind_approval(first["id"], "approval-7", "允许覆盖旧地图吗？")
    check(waiting["status"] == WAITING_APPROVAL and waiting["approval"]["id"] == "approval-7", "approval is bound")
    manager.resume_after_approval(first["id"])
    done = manager.complete(first["id"], "地图已下载")
    check(done["status"] == COMPLETED and done["result"] == "地图已下载", "task completes")

    manager.start(third["id"])
    cancelled = manager.cancel(third["id"], "用户取消")
    check(cancelled["status"] == CANCELLED, "task cancels without executor control")
    try:
        manager.complete(third["id"])
    except TaskError:
        pass
    else:
        check(False, "terminal state transition is rejected")

    fourth = manager.create("同步备份", "同步相册备份")
    manager.start(fourth["id"])
    manager.bind_approval(fourth["id"], "approval-8", "允许覆盖远端备份吗？")
    restored = TaskManager.restore(journal)
    check(
        restored.get(first["id"])["status"] == COMPLETED
        and restored.get(third["id"])["status"] == CANCELLED
        and restored.get(second["id"])["status"] == INTERRUPTED
        and restored.get(fourth["id"])["status"] == INTERRUPTED,
        "restore interrupts running and waiting tasks",
    )
    interrupted = restored.get(second["id"])
    check(interrupted["error"] == "管家重启中断，未自动继续" and restored.summary()["running_slots"] == 0,
          "interrupted task is visible and consumes no slot")
    check(len(restored.summary()["interrupted_tasks"]) == 2, "summary lists interrupted tasks")
    check(restored.retry_interrupted(second["id"])["status"] == QUEUED, "retry requires explicit requeue")
    check(journal.read_text(encoding="utf-8").count("\n") >= 12, "restore interruption is persisted")

print("ALL PASS test_task_manager")
