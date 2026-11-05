"""Worker 全链路冒烟（真实服务）：TaskExecutor(ts) → agent-runtime 18800 → LangGraph → outcome。

前提：agent-runtime(18800) 与 bridge(18801) 已在跑；models.json 指向智谱直连。
跑一个不需要工具的小任务，验证 create run → SSE → outcome completed → 任务完成落库。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ['GUANJIA_WORKER_BACKEND'] = 'ts'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.announcement import AnnouncementPolicy  # noqa: E402
from guanjia.task_executor import TaskWorkerExecutor  # noqa: E402
from guanjia.task_manager import TaskManager  # noqa: E402
from guanjia.task_service import TaskService  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    service = TaskService(TaskManager(Path(tmp) / 'tasks.jsonl'))
    notices = []
    executor = TaskWorkerExecutor(
        service, policy=AnnouncementPolicy(lambda: True), on_notice=notices.append)
    task = service.task_start('冒烟', '这是一次链路冒烟测试。不需要调用任何工具，直接在最终回复里给出一句确认：链路畅通。')
    executor.submit(task['id'])
    deadline = time.time() + 120
    status = ''
    while time.time() < deadline:
        status = service.task_status(task['id'])['status']
        if status in {'completed', 'failed', 'cancelled'}:
            break
        time.sleep(0.5)
    final = service.task_status(task['id'])
    print('status:', status)
    print('result:', final.get('result'))
    print('error:', final.get('error'))
    executor.shutdown()
    if status != 'completed':
        raise SystemExit('worker smoke FAILED')
    print('WORKER SMOKE PASS')
