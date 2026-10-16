"""O5 worker fixture：独立会话、并发、失败、协作取消和影音静默通知。"""

import tempfile
import threading
import time
from pathlib import Path

from guanjia.announcement import AnnouncementPolicy
from guanjia.task_executor import TaskWorkerExecutor
from guanjia.task_manager import ConcurrencyLimitError, TaskManager
from guanjia.task_service import TaskService


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


class Brain:
    def __init__(self, key, reply, started=None, release=None):
        self.key, self.reply, self.started, self.release = key, reply, started, release

    def ask(self, _prompt):
        if self.started:
            self.started.set()
        if self.release:
            self.release.wait(2)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


with tempfile.TemporaryDirectory() as tmp:
    service = TaskService(TaskManager(Path(tmp) / "tasks.jsonl"))
    notices = []
    executor = TaskWorkerExecutor(
        service,
        brain_factory=lambda key: Brain(key, "TASK_OUTCOME: completed - 已完成"),
        policy=AnnouncementPolicy(lambda: True),
        on_notice=notices.append,
    )
    first = service.task_start("资料", "只处理资料")
    session = executor.submit(first["id"])
    for _ in range(40):
        if service.task_status(first["id"])["status"] == "completed":
            break
        time.sleep(0.02)
    check(session.session_key == first["session_key"], "worker uses isolated task session")
    check(service.task_status(first["id"])["status"] == "completed", "explicit outcome completes task")
    check(notices[-1].kind == "task_completed" and not notices[-1].speak, "quiet policy keeps completion as notification")

    trailing = service.task_start("尾行协议", "完成标记必须在最后一行")
    last_line = TaskWorkerExecutor(service, brain_factory=lambda key: Brain(key, "工具已完成。\nTASK_OUTCOME: completed - 尾行结果"))
    last_line.submit(trailing["id"])
    for _ in range(40):
        if service.task_status(trailing["id"])["status"] == "completed":
            break
        time.sleep(0.02)
    check(service.task_status(trailing["id"])["status"] == "completed", "last-line completion protocol is accepted")

    failed = service.task_start("失败", "测试失败")
    bad = TaskWorkerExecutor(service, brain_factory=lambda key: Brain(key, RuntimeError("gateway down")))
    bad.submit(failed["id"])
    for _ in range(40):
        if service.task_status(failed["id"])["status"] == "failed":
            break
        time.sleep(0.02)
    check(service.task_status(failed["id"])["status"] == "failed", "worker failure is persisted")

    started, release = threading.Event(), threading.Event()
    busy = TaskWorkerExecutor(service, brain_factory=lambda key: Brain(key, "TASK_OUTCOME: completed - 不应完成", started, release))
    one, two, three = (service.task_start(f"任务{i}", "等待") for i in range(3))
    busy.submit(one["id"])
    busy.submit(two["id"])
    started.wait(1)
    try:
        busy.submit(three["id"])
    except ConcurrencyLimitError:
        check(True, "manager enforces two worker slots")
    cancel = busy.request_cancel(one["id"], "用户取消")
    check(cancel["status"] == "cancelled", "cancel is cooperative and records state")
    release.set()
    time.sleep(0.1)
    check(service.task_status(one["id"])["status"] == "cancelled", "cancelled worker never reports completion")
    executor.shutdown()
    last_line.shutdown()
    bad.shutdown()
    busy.shutdown()

print("ALL PASS test_task_executor")
