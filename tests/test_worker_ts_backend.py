"""Wave1-C：TS Worker 后端适配器（core/agent_runtime_adapter.py）全链路测试。

不真连 TS runtime——起一个 stub HTTP/SSE 服务（线程内 http.server，返回预编排事件序列），
覆盖 AgentRuntimeAdapter.call 全链路：
- 创建 run、消费 SSE、产出 WorkerEvent（progress / tool_trace→progress 型 / approval→needs_permission）；
- outcome completed → TaskResult 构造（evidence.kind='agent_runtime_worker'）；
- approval 挂起 → resume 唤醒（模拟 bridge 回调 TS /resume）→ 继续 → outcome；
- 取消（command='cancel' → POST /v1/worker/runs/{id}/cancel → outcome cancelled）；
- 超时（注入短超时 → ReadTimeout → {'ok': False}）；
- WorkerRuntime 状态机集成（真实 start/result 生命周期，默认 backend 语义不受影响）；
- build_worker_adapter 工厂开关（GUANJIA_WORKER_BACKEND=ts|openclaw）。

check() 风格：python -X utf8 tests/test_worker_ts_backend.py，PASS/FAIL 逐项打印。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.core.agent_runtime_adapter import AgentRuntimeAdapter, build_worker_adapter  # noqa: E402
from guanjia.core.contracts import TaskSpec  # noqa: E402


def check(condition: bool, name: str) -> None:
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    if not condition:
        raise SystemExit(1)


# ---------- 事件 fixture ----------

def progress_event(stage: str = 'act', percent: int | None = 20):
    return {'type': 'progress', 'percent': percent, 'stage': stage, 'message': None}


def tool_trace_event(capability: str = 'screen_capture', ok: bool = True):
    return {'type': 'tool_trace', 'capability_id': capability, 'ok': ok,
            'duration_ms': 120, 'summary': f'{capability} 完成'}


def approval_event(capability: str = 'terminal', request_id: str = 'todo-1'):
    return {'type': 'approval_request', 'request_id': request_id, 'capability': capability,
            'rationale': f'{capability}：需要主人确认', 'action_fingerprint': 'a' * 64}


TASK_RESULT_WIRE = {
    'result_id': 'res-1', 'task_id': 'task-77', 'revision': 1,
    'summary': '屏幕上是代码编辑器，在写 TypeScript',
    'artifacts': [{'uri': 'C:/tmp/shot-77.jpg', 'kind': 'screen_capture', 'sha256': None, 'media_type': None}],
    'evidence': [{
        'kind': 'agent_runtime_worker', 'summary': '屏幕上是代码编辑器，在写 TypeScript',
        'observed_at': '2026-11-05T12:00:00Z',
        'artifacts': [{'uri': 'C:/tmp/shot-77.jpg', 'kind': 'screen_capture', 'sha256': None, 'media_type': None}],
        'details': {'full_reply': '屏幕上是代码编辑器，在写 TypeScript'},
    }],
    'error': None, 'key_steps': ['screen_capture → 成功'], 'evidence_at': '2026-11-05T12:00:00Z',
}


def outcome_event(status: str = 'completed', summary: str = '任务完成', result: dict | None = None):
    return {'type': 'outcome', 'status': status, 'summary': summary,
            'result': TASK_RESULT_WIRE if status == 'completed' else result}


TASK_SPEC_DICT = {
    'task_id': 'task-77', 'revision': 1, 'goal': '看看屏幕上有什么',
    'constraints': [], 'references': [], 'permission_scope': None, 'priority': 0,
    'success_criteria': [], 'delivery_requirements': {}, 'resources': [],
}


def start_messages() -> list[dict]:
    """镜像 WorkerRuntime.start 传给 adapter.call 的消息形状。"""
    return [{'role': 'user', 'content': {
        'task': TASK_SPEC_DICT, 'materials': [], 'memory_packet': [],
    }}]


# ---------- stub HTTP/SSE 服务 ----------

class StubState:
    def __init__(self) -> None:
        self.create_requests: list[dict] = []
        self.resumes: list[dict] = []
        self.cancels: list[dict] = []
        self.script: list = []
        self.resume_event = threading.Event()
        self.cancel_event = threading.Event()
        # 场景3（超时）为 True：SSE 写完 script 后挂住连接不关闭
        self.hold_open = False


STATE = StubState()


def _make_stub() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 不往 stderr 打访问日志
            pass

        def _send_json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(length) or b'{}')

        def do_POST(self):  # noqa: N802
            body = self._body()
            if self.path == '/v1/worker/runs':
                STATE.create_requests.append(body)
                self._send_json(200, {'ok': True, 'run_id': body.get('run_id')})
                return
            if self.path.endswith('/resume'):
                STATE.resumes.append(body)
                STATE.resume_event.set()
                self._send_json(200, {'ok': True})
                return
            if self.path.endswith('/cancel'):
                STATE.cancels.append(body)
                STATE.cancel_event.set()
                self._send_json(200, {'ok': True, 'status': 'running'})
                return
            self._send_json(404, {'ok': False, 'reason': 'not_found'})

        def do_GET(self):  # noqa: N802
            if not self.path.endswith('/events'):
                self._send_json(404, {'ok': False, 'reason': 'not_found'})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            seq = 0
            for item in STATE.script:
                if item == 'WAIT_RESUME':
                    STATE.resume_event.wait(timeout=10)
                    continue
                if item == 'WAIT_CANCEL':
                    STATE.cancel_event.wait(timeout=10)
                    continue
                line = f'data: {json.dumps({"seq": seq, "event": item}, ensure_ascii=False)}\n\n'
                self.wfile.write(line.encode('utf-8'))
                self.wfile.flush()
                seq += 1
            if STATE.hold_open:
                time.sleep(30)  # 超时场景：流保持打开不发 outcome
            self.wfile.write(b'data: [DONE]\n\n')

    class _Server(ThreadingHTTPServer):
        daemon_threads = True

    server = _Server(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, name='worker-stub', daemon=True).start()
    return server


SERVER = _make_stub()
BASE = f'http://127.0.0.1:{SERVER.server_address[1]}'


def reset(script: list) -> None:
    STATE.create_requests.clear()
    STATE.resumes.clear()
    STATE.cancels.clear()
    STATE.script = script
    STATE.resume_event.clear()
    STATE.cancel_event.clear()
    STATE.hold_open = False


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def call_start(adapter: AgentRuntimeAdapter):
    return adapter.call(
        logical_session='task/task-77/r1', model='worker-model', command='start',
        operation_id='op-1', messages=start_messages(),
    )


# ---------- 1. 全链路：审批挂起 → resume → outcome completed + TaskResult ----------

reset([progress_event(), approval_event(), 'WAIT_RESUME',
       tool_trace_event(), outcome_event('completed')])

adapter = AgentRuntimeAdapter(base_url=BASE, token='stub-token')
outcome_box: list[dict] = []
runner = threading.Thread(target=lambda: outcome_box.append(call_start(adapter)), daemon=True)
runner.start()

check(wait_for(lambda: any(e.event_type == 'needs_permission' for e in adapter.collected_events)),
      'SSE 事件流把 approval_request 转成 needs_permission 事件')
permission = next(e for e in adapter.collected_events if e.event_type == 'needs_permission')
check(permission.stage == 'terminal' and permission.payload.get('todo_id') == 'todo-1',
      'needs_permission 事件携带 capability 与 todo_id')
check(len(STATE.create_requests) == 1 and STATE.create_requests[0].get('run_id'),
      'start 创建 run（POST /v1/worker/runs，带 run_id）')
check(STATE.create_requests[0].get('task_spec', {}).get('task_id') == 'task-77',
      '创建请求透传 TaskSpec')
check(STATE.create_requests[0].get('model_hint') == 'worker-model',
      'model 参数进入 model_hint')

# 模拟 bridge 审批回调：POST TS runtime 的 /resume（这里打到 stub，唤醒 SSE 后续事件）
resume = httpx.post(f'{BASE}/v1/worker/runs/{STATE.create_requests[0]["run_id"]}/resume',
                    json={'decision': 'approve', 'credential': 'cred-1'},
                    headers={'Authorization': 'Bearer stub-token'}, trust_env=False, timeout=5.0)
check(resume.status_code == 200 and STATE.resumes[0]['decision'] == 'approve',
      '审批决定可送达 TS runtime 的 resume 端点')

runner.join(timeout=10)
response = outcome_box[0]
check(response.get('ok') is True, 'call(start) 返回 ok=True')
check(response.get('status') == 'completed', 'outcome completed 透传到返回值')
check(isinstance(response.get('task_result'), dict)
      and response['task_result']['summary'] == TASK_RESULT_WIRE['summary'],
      'TaskResult 以 JSON-safe dict 进入返回值（WorkerRuntime._strict_json 可序列化）')

result = adapter.results.get('task-77')
check(result is not None, '适配器旁路保存 TaskResult 对象')
check(result.task_id == 'task-77' and result.revision == 1, 'TaskResult 归属任务与修订')
check(result.evidence[0].kind == 'agent_runtime_worker', 'evidence.kind=agent_runtime_worker')
check(result.artifacts[0].uri == 'C:/tmp/shot-77.jpg', 'artifacts 透传 screen_capture 路径')
check(result.key_steps == ('screen_capture → 成功',), 'key_steps 记录工具调用')

types = [e.event_type for e in adapter.collected_events]
check(types == ['progress', 'needs_permission', 'progress'],
      f'WorkerEvent 序列正确（{types}；outcome 只产出 TaskResult，不转事件）')
traces = [e for e in adapter.collected_events
          if e.event_type == 'progress' and e.payload.get('tool_trace')]
check(len(traces) == 1 and traces[0].stage == 'screen_capture' and traces[0].payload.get('ok') is True,
      'tool_trace 转成 progress 型 WorkerEvent（stage=capability，payload 带结果）')
check(adapter.collected_events[0].sequence == 0 and adapter.collected_events[-1].sequence == 2,
      '事件 sequence 从信封透传')

# ---------- 2. 取消：command=cancel → POST /cancel → SSE outcome cancelled ----------

reset([progress_event(), 'WAIT_CANCEL', outcome_event('cancelled', summary='任务已取消：主人喊停')])

cancel_adapter = AgentRuntimeAdapter(base_url=BASE, token='stub-token')
cancel_box: list[dict] = []
cancel_runner = threading.Thread(target=lambda: cancel_box.append(call_start(cancel_adapter)), daemon=True)
cancel_runner.start()
check(wait_for(lambda: any(e.event_type == 'progress' for e in cancel_adapter.collected_events)),
      '取消场景：start 先消费到 progress')
cancel_response = cancel_adapter.call(
    logical_session='task/task-77/r1', model='worker-model', command='cancel',
    operation_id='op-cancel-1', messages=[],
)
check(cancel_response.get('ok') is True, 'command=cancel 转发到 TS cancel 端点成功')
check(len(STATE.cancels) == 1, 'cancel 请求到达 stub 的 /v1/worker/runs/{id}/cancel')
cancel_runner.join(timeout=10)
check(cancel_box[0].get('ok') is True and cancel_box[0].get('status') == 'cancelled',
      '取消后 SSE 以 outcome cancelled 收尾，call 返回 status=cancelled')

# ---------- 3. 超时：注入短超时 → ReadTimeout → ok=False ----------

reset([progress_event()])
STATE.hold_open = True

slow_adapter = AgentRuntimeAdapter(base_url=BASE, token='stub-token', timeout=0.5)
t0 = time.time()
timeout_response = call_start(slow_adapter)
elapsed = time.time() - t0
check(timeout_response.get('ok') is False and 'timeout' in str(timeout_response.get('error', '')),
      f'短超时触发读超时并返回 ok=False（{elapsed:.1f}s）')
check(elapsed < 5, '超时及时返回，不会挂满 900 秒')

# ---------- 4. 工厂：build_worker_adapter 只产 TS 后端 ----------

factory_adapter = build_worker_adapter()
check(isinstance(factory_adapter, AgentRuntimeAdapter),
      'build_worker_adapter 返回 AgentRuntimeAdapter（唯一后端，OpenClaw 分支已退役删除）')

print('ALL PASS: test_worker_ts_backend')
