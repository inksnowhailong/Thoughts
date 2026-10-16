"""O3 接线：任务接口不阻塞主线，也不会自动派发模型或工具。"""

import tempfile
from pathlib import Path

from guanjia.task_manager import QUEUED, RUNNING, TaskError, TaskManager
from guanjia.task_service import TaskService


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp:
    service = TaskService(TaskManager(Path(tmp) / "tasks.jsonl"))
    task = service.task_start("下载资料", "获取用户指定的资料")
    check(task["status"] == QUEUED, "task_start only queues and returns immediately")
    check(task["session_key"].startswith("agent:main:task-"), "task gets an isolated worker session key")
    check(service.task_status(task["id"])["id"] == task["id"], "task_status reads one task")
    check(service.task_status()["counts"][QUEUED] == 1, "task_status exposes summary")
    try:
        service.task_report(task["id"], "50%", 50)
    except TaskError:
        check(True, "queued task cannot report progress")
    else:
        check(False, "queued task cannot report progress")
    worker = service.workers.reserve(task["id"])
    check(worker.task_id == task["id"] and worker.session_key == task["session_key"], "explicit worker reserve claims the task")
    check(service.task_status(task["id"])["status"] == RUNNING, "reserve occupies a running slot")
    check(service.task_report(task["id"], "已完成准备", 10)["progress_percent"] == 10, "running worker reports progress")
    check(service.task_cancel(task["id"], "用户取消")["status"] == "cancelled", "cancel records request without process control")

    from guanjia.voice import VoiceDaemon

    daemon = object.__new__(VoiceDaemon)
    daemon.tasks = service
    from_voice = daemon.task_start("整理文件", "整理用户指定目录")
    check(from_voice["status"] == QUEUED and daemon.task_status(from_voice["id"])["id"] == from_voice["id"],
          "voice exposes the controlled task interface")

print("ALL PASS test_task_service")
