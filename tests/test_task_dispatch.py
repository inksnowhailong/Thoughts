"""O5b 调度闩锁：只领取本轮 task_start 新项，首句失败/打断则不执行。"""

import tempfile
import threading
import time
from pathlib import Path

from guanjia.task_dispatch import TaskDispatchScheduler
from guanjia.task_executor import TaskWorkerExecutor
from guanjia.task_manager import TaskManager
from guanjia.task_service import TaskService


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


class NoopBrain:
    def __init__(self, _key):
        pass

    def ask(self, _prompt):
        return "TASK_OUTCOME: completed - fixture"

release = threading.Event()


class BlockingBrain(NoopBrain):
    def ask(self, _prompt):
        release.wait(1)
        return "TASK_OUTCOME: completed - fixture"


with tempfile.TemporaryDirectory() as tmp:
    service = TaskService(TaskManager(Path(tmp) / "tasks.jsonl"))
    events, submitted = [], []
    gate = TaskDispatchScheduler(lambda *entry: events.append(entry))

    old = service.task_start("旧任务", "绝不自动领取")
    created = service.task_start("新任务", "本轮 task_start")
    check(gate.record_start("turn-1", created["id"]), "only new task_start can be recorded")
    gate.finish_turn("turn-1", reply_delivered=False, submit=submitted.append)
    check(not submitted and service.task_status(created["id"])["status"] == "queued", "failed first reply keeps new task queued")

    started = service.task_start("可派发", "首句播完")
    gate.record_start("turn-2", started["id"])
    gate.finish_turn("turn-2", reply_delivered=True, submit=submitted.append)
    check(submitted == [started["id"]], "successful main reply dispatches only recorded task")
    check(old["id"] not in submitted, "historical queued task is never scanned")

    interrupted = service.task_start("被打断", "仍排队")
    gate.record_start("turn-3", interrupted["id"])
    gate.finish_turn("turn-3", reply_delivered=False, submit=submitted.append)
    check(interrupted["id"] not in submitted, "interrupted reply never dispatches")

    # TaskManager 仍裁决两个槽位，调度器不自行扩容或换会话。
    limited = TaskService(TaskManager(Path(tmp) / "limited.jsonl", max_concurrent=2))
    executor = TaskWorkerExecutor(limited, brain_factory=BlockingBrain)
    ids = [limited.task_start(f"并发{i}", "fixture")["id"] for i in range(3)]
    for task_id in ids:
        gate.record_start("turn-4", task_id)
    gate.finish_turn("turn-4", reply_delivered=True, submit=executor.submit)
    statuses = [limited.task_status(task_id)["status"] for task_id in ids]
    check(statuses.count("queued") == 1, "third dispatch stays queued at concurrency limit")
    release.set()
    for _ in range(50):
        if all(limited.task_status(task_id)["status"] != "running" for task_id in ids[:2]):
            break
        time.sleep(0.02)
    executor.shutdown()

print("ALL PASS test_task_dispatch")
