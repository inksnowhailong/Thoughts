"""面板任务 DTO 不得泄露 worker 内部字段。"""

from types import SimpleNamespace

from guanjia.voice import VoiceDaemon


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


raw = {
    "counts": {"queued": 1}, "max_concurrent": 2, "running_slots": 0,
    "active_tasks": [], "interrupted_tasks": [],
    "recent_tasks": [{
        "id": "1234567890abcdef", "summary": "整理资料", "goal": "绝密目标", "session_key": "agent:main:task-secret",
        "status": "queued", "progress": "", "result": "", "error": "", "created_at": 1, "updated_at": 2,
        "approval": {"id": "pending-secret", "description": "删除文件", "created_at": 1, "expires_at": 3},
    }],
}
daemon = object.__new__(VoiceDaemon)
daemon.tasks = SimpleNamespace(task_status=lambda: raw)
dto = daemon._panel_tasks()
task = dto["recent_tasks"][0]
check(task["id"] == "90abcdef" and task["summary"] == "整理资料", "panel keeps short task identity and summary")
check("goal" not in task and "session_key" not in task and "pending-secret" not in str(dto), "panel strips worker and approval internals")
check(task["approval"] == {"description": "删除文件", "expires_at": 3}, "panel keeps approval summary only")
print("ALL PASS test_panel_task_dto")
