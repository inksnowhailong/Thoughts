"""TS agent-runtime Worker 后端适配器（GUANJIA_WORKER_BACKEND=ts）。

实现 WorkerAdapter 协议（core/contracts.py：session_for / call），
把任务命令映射到 TS Runtime 的 /v1/worker/*：

- command='start'  → POST /v1/worker/runs + 消费 SSE 事件流直到 outcome；
- command='cancel' → POST /v1/worker/runs/{run_id}/cancel；
- 其余生命周期命令（progress/subtask/pause/resume/result）在 TS 后端由
  worker 运行时自主推进，适配器直接确认（{'ok': True, 'ignored': command}）。

SSE 事件 → WorkerEvent（契约 core/contracts.py::WorkerEvent）：
- progress        → WorkerEvent.progress(task_id, revision, stage, percent)
- tool_trace      → progress 型 WorkerEvent（stage=capability_id，payload 带 ok/duration_ms/summary）
- approval_request→ needs_permission 事件（payload 带 todo_id/rationale/fingerprint）
- outcome         → TaskResult 构造（evidence.kind='agent_runtime_worker'），
                    存 self.results[task_id] 并以 JSON-safe dict 进入 call() 返回值。

超时惯例：读流长超时默认 900 秒（brain.py 的 STREAM_READ_TIMEOUT_SEC 同源），
审批挂起期间同一条 SSE 连接继续等 outcome；可注入短超时用于测试。
环境变量：GUANJIA_WORKER_URL（缺省 http://127.0.0.1:18800）、
GUANJIA_BRIDGE_TOKEN（缺省读 agent-runtime/config/runtime.json 的 token 字段）。
"""

from __future__ import annotations

import json
import os
import uuid
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Callable, Mapping, Sequence

import httpx

from .contracts import ArtifactRef, Evidence, StructuredError, TaskResult, WorkerEvent

DEFAULT_WORKER_URL = 'http://127.0.0.1:18800'
STREAM_READ_TIMEOUT_SEC = 900.0
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_token() -> str:
    env = os.environ.get('GUANJIA_BRIDGE_TOKEN', '').strip()
    if env:
        return env
    runtime_json = _REPO_ROOT / 'agent-runtime' / 'config' / 'runtime.json'
    try:
        parsed = json.loads(runtime_json.read_text(encoding='utf-8'))
        token = parsed.get('token') if isinstance(parsed, dict) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    except (OSError, ValueError):
        pass
    return ''


def _as_utc(text: str):
    from datetime import UTC, datetime

    return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(UTC)


def _evidence_from_wire(wire: Mapping[str, object]) -> Evidence:
    return Evidence(
        kind=str(wire.get('kind')),
        summary=str(wire.get('summary') or ''),
        observed_at=_as_utc(str(wire.get('observed_at'))),
        artifacts=tuple(ArtifactRef(**a) for a in (wire.get('artifacts') or [])),
        details=dict(wire.get('details') or {}),
    )


def _task_result_from_wire(task_id: str, revision: int, wire: Mapping[str, object]) -> TaskResult:
    error = wire.get('error')
    return TaskResult(
        result_id=str(wire.get('result_id') or uuid.uuid4().hex),
        task_id=task_id,
        revision=revision,
        summary=str(wire.get('summary') or ''),
        artifacts=tuple(ArtifactRef(**a) for a in (wire.get('artifacts') or [])),
        evidence=tuple(_evidence_from_wire(e) for e in (wire.get('evidence') or [])),
        error=None if error is None else StructuredError(
            code=str(error.get('code')), message=str(error.get('message')),
            retryable=bool(error.get('retryable')),
        ),
        key_steps=tuple(str(s) for s in (wire.get('key_steps') or [])),
    )


def _task_result_to_json_dict(result: TaskResult) -> dict[str, object]:
    """call() 返回值必须 strict JSON（调用方会再序列化一次）。"""
    return {
        'result_id': result.result_id,
        'task_id': result.task_id,
        'revision': result.revision,
        'summary': result.summary,
        'artifacts': [dict(a.__dict__) for a in result.artifacts],
        'evidence': [
            {
                'kind': e.kind, 'summary': e.summary,
                'observed_at': e.observed_at.isoformat(),
                'artifacts': [dict(a.__dict__) for a in e.artifacts],
                'details': {k: v for k, v in e.details.items()},
                'evidence_id': e.evidence_id,
            }
            for e in result.evidence
        ],
        'error': None if result.error is None else {
            'code': result.error.code, 'message': result.error.message,
            'retryable': result.error.retryable,
        },
        'key_steps': list(result.key_steps),
        'evidence_at': result.evidence_at.isoformat(),
    }


class AgentRuntimeAdapter:
    """WorkerAdapter 协议实现：调 TS agent-runtime 的 /v1/worker/*。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = STREAM_READ_TIMEOUT_SEC,
        event_sink: Callable[[WorkerEvent], None] | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get('GUANJIA_WORKER_URL', DEFAULT_WORKER_URL)).rstrip('/')
        self._token = token if token is not None else _resolve_token()
        self._timeout = timeout
        self._event_sink = event_sink
        # task_id → run_id（cancel 用）；task_id → TaskResult（worker 事件流外的旁路产物）
        self._runs: dict[str, str] = {}
        self.results: dict[str, TaskResult] = {}
        self.collected_events: list[WorkerEvent] = []
        self._lock = RLock()
        self._client = httpx.Client(
            trust_env=False,  # 本机回环，不走系统代理
            timeout=httpx.Timeout(timeout, connect=5.0),
        )

    # ---------- WorkerAdapter 协议 ----------

    def session_for(self, logical_session: str) -> str:
        """确定性映射：与 OpenClaw 版同为“稳定逻辑名 → 不透明会话标识”。"""
        digest = sha256(logical_session.encode('utf-8')).hexdigest()[:24]
        return f'ts:{digest}'

    def call(
        self,
        *,
        logical_session: str,
        model: str,
        command: str,
        operation_id: str | None = None,
        messages: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        if command == 'start':
            return self._start(logical_session, model, messages)
        if command == 'cancel':
            return self._cancel(logical_session)
        # progress/subtask/pause/resume/result 等由 TS worker 运行时自主推进
        return {'ok': True, 'backend': 'agent_runtime', 'ignored': command}

    def set_event_sink(self, sink: Callable[[WorkerEvent], None] | None) -> None:
        """运行期替换事件汇（如 Task Executor 把 WorkerEvent 映射成任务进度上报）。"""
        self._event_sink = sink

    def close(self) -> None:
        self._client.close()

    # ---------- start：创建 run + 消费 SSE ----------

    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self._token}'}

    def _emit(self, event: WorkerEvent) -> None:
        self.collected_events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    def _start(self, logical_session: str, model: str, messages: Sequence[Mapping[str, object]]) -> dict[str, object]:
        if not messages or not isinstance(messages[0], Mapping):
            return {'ok': False, 'error': 'start requires a task payload message'}
        content = messages[0].get('content')
        if not isinstance(content, Mapping):
            return {'ok': False, 'error': 'start message content must be a mapping'}
        task = content.get('task')
        if not isinstance(task, Mapping):
            return {'ok': False, 'error': 'start payload missing task spec'}
        task_id = str(task.get('task_id') or '')
        revision = int(task.get('revision') or 1)
        if not task_id:
            return {'ok': False, 'error': 'task spec missing task_id'}
        run_id = uuid.uuid4().hex
        body = {
            'run_id': run_id,
            'task_spec': dict(task),
            'materials': {'references': list(content.get('materials') or [])},
            'memory_packet': {'summaries': list(content.get('memory_packet') or [])},
            # 空数组 = TS 侧取全部 workerVisible 能力（见 runs.ts buildCtx）
            'allowed_capabilities': [],
            'model_hint': model or None,
        }
        try:
            response = self._client.post(
                f'{self._base_url}/v1/worker/runs', json=body, headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            return {'ok': False, 'error': f'worker backend unreachable: {exc}'}
        if response.status_code != 200:
            return {'ok': False, 'error': f'worker run create http_{response.status_code}'}
        with self._lock:
            self._runs[task_id] = run_id
        try:
            outcome = self._consume_events(run_id, task_id, revision)
        except httpx.TimeoutException as exc:
            return {'ok': False, 'error': f'worker stream timeout: {type(exc).__name__}'}
        except httpx.HTTPError as exc:
            return {'ok': False, 'error': f'worker stream failed: {exc}'}
        finally:
            with self._lock:
                self._runs.pop(task_id, None)
        if outcome is None:
            return {'ok': False, 'error': 'worker event stream closed without outcome'}
        status = str(outcome.get('status') or '')
        summary = str(outcome.get('summary') or '')
        result_dict: dict[str, object] | None = None
        if status == 'completed' and isinstance(outcome.get('result'), Mapping):
            result = _task_result_from_wire(task_id, revision, outcome['result'])
            with self._lock:
                self.results[task_id] = result
            result_dict = _task_result_to_json_dict(result)
        return {
            'ok': True,
            'backend': 'agent_runtime',
            'run_id': run_id,
            'status': status,
            'summary': summary,
            'task_result': result_dict,
        }

    def _consume_events(self, run_id: str, task_id: str, revision: int) -> Mapping[str, object] | None:
        """读 SSE 直到 outcome；progress/tool_trace/approval 转成 WorkerEvent。"""
        with self._client.stream(
            'GET', f'{self._base_url}/v1/worker/runs/{run_id}/events', headers=self._headers(),
        ) as stream:
            if stream.status_code != 200:
                raise httpx.HTTPStatusError(
                    f'events http_{stream.status_code}', request=stream.request, response=stream,
                )
            for line in stream.iter_lines():
                line = line.strip()
                if not line.startswith('data:'):
                    continue
                payload = line[5:].strip()
                if not payload or payload == '[DONE]':
                    continue
                envelope = json.loads(payload)
                seq = envelope.get('seq')
                event = envelope.get('event') or {}
                kind = event.get('type')
                if kind == 'progress':
                    self._emit(WorkerEvent.progress(
                        task_id, revision,
                        stage=str(event.get('stage') or 'run'),
                        percent=event.get('percent') if isinstance(event.get('percent'), int) else None,
                        sequence=seq if isinstance(seq, int) else None,
                    ))
                elif kind == 'tool_trace':
                    self._emit(WorkerEvent(
                        task_id, revision, 'progress',
                        stage=str(event.get('capability_id') or 'tool'),
                        percent=None,
                        payload={
                            'tool_trace': True,
                            'ok': bool(event.get('ok')),
                            'duration_ms': event.get('duration_ms'),
                            'summary': str(event.get('summary') or ''),
                        },
                        sequence=seq if isinstance(seq, int) else None,
                    ))
                elif kind == 'approval_request':
                    self._emit(WorkerEvent(
                        task_id, revision, 'needs_permission',
                        stage=str(event.get('capability') or 'worker'),
                        percent=None,
                        payload={
                            'todo_id': str(event.get('request_id') or ''),
                            'rationale': str(event.get('rationale') or ''),
                            'action_fingerprint': str(event.get('action_fingerprint') or ''),
                        },
                        sequence=seq if isinstance(seq, int) else None,
                    ))
                elif kind == 'outcome':
                    return event
        return None

    # ---------- cancel ----------

    def _cancel(self, logical_session: str) -> dict[str, object]:
        """logical_session = task/{task_id}/r{revision}。"""
        parts = logical_session.split('/')
        task_id = parts[1] if len(parts) > 1 else logical_session
        with self._lock:
            run_id = self._runs.get(task_id)
        if run_id is None:
            # TS 侧 cancel 幂等：不存在的 run 视为已结束
            return {'ok': True, 'backend': 'agent_runtime', 'status': 'no_active_run'}
        try:
            response = self._client.post(
                f'{self._base_url}/v1/worker/runs/{run_id}/cancel',
                json={'reason': f'cancel from {logical_session}'}, headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            return {'ok': False, 'error': f'worker backend unreachable: {exc}'}
        if response.status_code != 200:
            return {'ok': False, 'error': f'worker cancel http_{response.status_code}'}
        body = response.json()
        return {'ok': True, 'backend': 'agent_runtime', 'status': str(body.get('status') or '')}


def build_worker_adapter() -> 'AgentRuntimeAdapter':
    """Worker 后端唯一工厂：TS agent-runtime。

    （OpenClaw 分支已随退役删除——生产从未实例化过 OpenClawAdapter，历史回滚路径是
    TaskExecutor 的 brain_factory Brain.ask，不是这个适配器。）
    """
    return AgentRuntimeAdapter()
