"""O4 任务 IPC：本机 capability、摘要脱敏与零执行语义。"""

import json
import tempfile
import time
from pathlib import Path

import httpx

from guanjia.task_ipc import TaskIpc
from guanjia.task_manager import TaskManager
from guanjia.task_service import TaskService


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp:
    descriptor = Path(tmp) / "task-ipc.json"
    started = []
    ipc = TaskIpc(TaskService(TaskManager(Path(tmp) / "tasks.jsonl")), descriptor, on_started=lambda task: started.append(task["id"]))
    ipc.start()
    try:
        cfg = json.loads(descriptor.read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{cfg['port']}"
        wrong = httpx.post(f"{base}/start", json={"summary": "下载", "goal": "执行命令"})
        check(wrong.status_code == 403, "wrong capability is refused")
        headers = {"X-Guanjia-Task-Cap": cfg["capability"]}
        created = httpx.post(f"{base}/start", headers=headers, json={"summary": "整理资料", "goal": "不应暴露的完整目标"}).json()
        task = created["task"]
        check(created["ok"] and task["status"] == "queued", "start only creates queued task")
        check(started == [task["id"]], "only successful task_start registers dispatch candidate")
        check("goal" not in task and "session_key" not in task, "IPC never exposes goal or worker session")
        service = ipc.tasks
        service.workers.reserve(task["id"])
        waiting = httpx.post(f"{base}/approval", headers=headers, json={
            "id": task["id"], "approval_id": "pending-1", "description": "删除文件", "expires_at": 1,
        }).json()
        check(waiting["ok"] and waiting["task"]["status"] == "waiting_approval", "guard approval binds waiting task")
        summary = httpx.get(f"{base}/status", headers=headers).json()
        check(summary["ok"] and summary["summary"]["counts"]["waiting_approval"] == 1, "status exposes only task summary")
        cancelled = httpx.post(f"{base}/cancel", headers=headers, json={"id": task["id"], "reason": "用户取消"}).json()
        check(cancelled["ok"] and cancelled["task"]["status"] == "cancelled", "cancel records request only")
        check(started == [task["id"]], "status cancel and approval never register dispatch")
    finally:
        ipc.stop()
    time.sleep(0.05)
    check(not descriptor.exists(), "descriptor is removed on stop")

print("ALL PASS test_task_ipc")
