"""Wave1-C 接线③：TaskWorkerExecutor 的 GUANJIA_WORKER_BACKEND=ts 分支。

不真连 TS runtime——注入假 WorkerAdapter（实现 session_for/call 协议），验证：
- backend=ts 时 start 命令 → 适配器 call → outcome completed → L1 验收（无 verify 直接过）→ 任务完成；
- outcome failed → 任务失败落库，失败原因带 summary；
- 适配器抛异常 → 任务失败（不挂死 worker 线程）；
- progress 事件经 set_event_sink 映射成 task_report；
- request_cancel 通知适配器取消，运行中任务以 cancelled 收场、worker 不再回写 completed；
- backend=openclaw（缺省）：不装适配器，Brain 路径行为不变（回归）。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 环境开关必须在 import guanjia.task_executor 之前置好（executor 在 __init__ 读）
os.environ['GUANJIA_WORKER_BACKEND'] = 'ts'

import sys  # noqa: E402

sys.path.insert(0, str(ROOT / 'src'))

from guanjia.announcement import AnnouncementPolicy  # noqa: E402
from guanjia.task_executor import TaskWorkerExecutor  # noqa: E402
from guanjia.task_manager import TaskManager  # noqa: E402
from guanjia.task_service import TaskService  # noqa: E402


def check(condition: bool, name: str) -> None:
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    if not condition:
        raise SystemExit(1)


class FakeTsAdapter:
    """WorkerAdapter 协议假件：start 返回预编排 outcome；cancel 记录调用。"""

    def __init__(self, outcome: dict, *, fail_with: Exception | None = None, block: threading.Event | None = None):
        self.outcome = outcome
        self.fail_with = fail_with
        self.block = block
        self.cancel_calls: list[str] = []
        self.sink = None
        self.lock = threading.Lock()

    def session_for(self, logical_session: str) -> str:
        return f'ts-fake:{logical_session}'

    def set_event_sink(self, sink) -> None:
        self.sink = sink

    def call(self, *, logical_session: str, model: str, command: str, operation_id=None, messages=()):
        if command == 'cancel':
            with self.lock:
                self.cancel_calls.append(logical_session)
            if self.block is not None:
                self.block.set()  # 让挂着的 start 得以返回 cancelled
            return {'ok': True, 'status': 'cancelled'}
        if self.fail_with is not None:
            raise self.fail_with
        # start：先发两个 progress 事件（模拟 SSE），再阻塞等 cancel（如需要），最后给 outcome
        task_id = str((messages[0]['content']['task']['task_id']))
        if self.sink is not None:
            from datetime import UTC, datetime

            from guanjia.core.contracts import WorkerEvent

            self.sink(WorkerEvent(task_id, 1, 'progress', 'plan', datetime.now(UTC).replace(microsecond=0), 20))
            self.sink(WorkerEvent(task_id, 1, 'progress', 'act', datetime.now(UTC).replace(microsecond=0), 55))
        if self.block is not None:
            self.block.wait(2)
            return {'ok': True, 'backend': 'agent_runtime', 'status': 'cancelled', 'summary': '按请求取消'}
        return dict(self.outcome)


def wait_status(service: TaskService, task_id: str, status: str, timeout: float = 3.0) -> bool:
    for _ in range(int(timeout / 0.02)):
        if service.task_status(task_id)['status'] == status:
            return True
        time.sleep(0.02)
    return False


OUTCOME_COMPLETED = {
    'ok': True, 'backend': 'agent_runtime', 'status': 'completed',
    'summary': '屏幕上是代码编辑器',
    'task_result': None,
}

with tempfile.TemporaryDirectory() as tmp:
    service = TaskService(TaskManager(Path(tmp) / 'tasks.jsonl'))
    notices: list = []

    # 1) completed：进度上报 + 任务完成
    adapter = FakeTsAdapter(OUTCOME_COMPLETED)
    executor = TaskWorkerExecutor(
        service, policy=AnnouncementPolicy(lambda: True), on_notice=notices.append,
        worker_adapter=adapter,
    )
    check(executor._backend == 'ts', 'backend=ts activates adapter path')
    t1 = service.task_start('总结屏幕', '看一眼屏幕说了什么')
    executor.submit(t1['id'])
    check(wait_status(service, t1['id'], 'completed'), 'ts outcome completed finishes task')
    done = service.task_status(t1['id'])
    check('代码编辑器' in done['result'], 'summary from TS outcome lands in task result')
    check(any(n.kind == 'task_completed' for n in notices), 'completion notice emitted')
    reports = service.task_status(t1['id'])
    check(isinstance(reports['progress'], str) and reports['progress'] in {'act', 'plan'},
          'worker progress events mapped into task report')
    executor.shutdown()

    # 3) failed outcome：任务失败落库
    service2 = TaskService(TaskManager(Path(tmp) / 'tasks2.jsonl'))
    adapter_fail = FakeTsAdapter({'ok': True, 'status': 'failed', 'summary': '工具全部不可用'})
    executor2 = TaskWorkerExecutor(service2, worker_adapter=adapter_fail)
    t2 = service2.task_start('会失败', '故意失败')
    executor2.submit(t2['id'])
    check(wait_status(service2, t2['id'], 'failed'), 'ts outcome failed fails task')
    check('工具全部不可用' in service2.task_status(t2['id'])['error'], 'failure reason carries summary')
    executor2.shutdown()

    # 4) 适配器异常：任务失败且不挂死
    service3 = TaskService(TaskManager(Path(tmp) / 'tasks3.jsonl'))
    adapter_boom = FakeTsAdapter(OUTCOME_COMPLETED, fail_with=RuntimeError('backend down'))
    executor3 = TaskWorkerExecutor(service3, worker_adapter=adapter_boom)
    t3 = service3.task_start('后端炸', '异常处理')
    executor3.submit(t3['id'])
    check(wait_status(service3, t3['id'], 'failed'), 'adapter exception fails task cleanly')
    check('backend down' in service3.task_status(t3['id'])['error'], 'exception message persisted')
    executor3.shutdown()

    # 5) 运行中取消：cancel 命令到达适配器，任务 cancelled，不回写 completed
    service4 = TaskService(TaskManager(Path(tmp) / 'tasks4.jsonl'))
    gate = threading.Event()
    adapter_blocking = FakeTsAdapter(OUTCOME_COMPLETED, block=gate)
    executor4 = TaskWorkerExecutor(service4, worker_adapter=adapter_blocking)
    t4 = service4.task_start('长任务', '会被取消')
    executor4.submit(t4['id'])
    for _ in range(100):
        if adapter_blocking.sink is not None:
            break
        time.sleep(0.02)
    cancelled = executor4.request_cancel(t4['id'], '主人取消')
    check(cancelled['status'] == 'cancelled', 'cancel transitions task immediately')
    check(wait_status(service4, t4['id'], 'cancelled'), 'task stays cancelled')
    time.sleep(0.2)
    check(service4.task_status(t4['id'])['status'] == 'cancelled', 'late outcome never overwrites cancellation')
    check(len(adapter_blocking.cancel_calls) == 1 and t4['id'] in adapter_blocking.cancel_calls[0],
          'cancel command forwarded to adapter with task session')
    executor4.shutdown()

print('ALL PASS test_task_executor_ts_backend')
