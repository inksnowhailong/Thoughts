"""P1 面板任务摘要：只验证 status/渲染契约，不启动音频或执行任务。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from guanjia.events import EventHub
from guanjia.panel import PANEL_HTML, PanelBackend
from guanjia.settings import SettingsStore
from guanjia.task_manager import TaskManager


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp:
    manager = TaskManager(Path(tmp) / "tasks.jsonl")
    completed = manager.create("下载资料", "下载主人指定的资料")
    manager.start(completed["id"])
    manager.complete(completed["id"], "资料已下载")
    running = manager.create("整理照片", "按月份整理照片")
    manager.start(running["id"])
    manager.report_progress(running["id"], "正在归档", 40)
    queued = manager.create("清理缓存", "清理浏览器缓存")
    summary = manager.summary()

    check(summary["counts"]["queued"] == 1 and summary["counts"]["running"] == 1, "summary retains real queue and running counts")
    recent = {task["id"]: task for task in summary["recent_tasks"]}
    check(recent[completed["id"]]["result"] == "资料已下载" and recent[running["id"]]["progress_percent"] == 40,
          "summary exposes recent result and progress read-only")

    status = {"view": "listening", "label": "在听", "tasks": summary}
    panel = PanelBackend(EventHub(), SettingsStore(None), status=lambda: status,
                         control=lambda action: (True, action), inject=lambda text, dry_run: {})
    code, body = panel.route_get("/api/status", {})
    check(code == 200 and body["ok"] and body["status"]["tasks"] == summary, "panel read-only status route preserves TaskManager summary")

html = PANEL_HTML.read_text(encoding="utf-8")
for marker in ('id="taskDetails"', 'id="taskCounts"', 'id="taskList"', "renderTaskSummary", "TASK_STATE", 'getJson("/api/status")'):
    check(marker in html, f"panel contains {marker}")
check("task_start" not in html and "task_cancel" not in html, "panel contains no task write action")
check("O1 IPC 审批卡" in html and "现有 PendingConfirm" in html, "O1 remains separate from existing PendingConfirm")

print("ALL PASS test_p1_panel_tasks")
