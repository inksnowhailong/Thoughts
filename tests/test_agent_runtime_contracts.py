"""Phase 0 任务B:双端共享契约 fixtures 的 Python 侧 round-trip 验证。

与 agent-runtime/test/contracts/fixtures.roundtrip.test.ts 消费同一组
contracts/fixtures/*.json,保证 TS(Zod)与 Python(dataclass)解析一致。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.contracts.events import EventEnvelope
from guanjia.core.contracts import (
    ArtifactRef,
    Evidence,
    StructuredError,
    TaskResult,
    WorkerEvent,
    task_spec_from_dict,
    task_spec_to_dict,
)
from guanjia.core.contracts import MemoryPacket
from guanjia.permission.contracts import (
    ActorEvidence,
    AuthorizationRequest,
    PermissionDecision,
    PermissionOutcome,
    Reversibility,
)
from guanjia.permission.roles import Role

FIXTURES = ROOT / 'contracts' / 'fixtures'


def load_fixture(*segments: str) -> dict:
    return json.loads(FIXTURES.joinpath(*segments).read_text(encoding='utf-8'))


def parse_utc(value: str) -> datetime:
    """与 contracts/events.py::_parse_utc 相同的 Z 后缀解析。"""
    return datetime.fromisoformat(value[:-1] + '+00:00')


def test_event_envelope_roundtrip() -> None:
    data = load_fixture('event-envelope.json')
    envelope = EventEnvelope.from_dict(data)
    assert envelope.schema_version == 1
    assert envelope.event_type == 'task.dispatched'
    assert envelope.causation_id is None
    assert envelope.to_dict() == data


def test_task_spec_roundtrip() -> None:
    data = load_fixture('task-spec.json')
    spec = task_spec_from_dict(data)
    assert spec.task_id == 'task-2041'
    assert spec.permission_scope is not None
    assert spec.permission_scope.grants == ('photos.read', 'render.slides')
    assert task_spec_to_dict(spec) == data


def test_worker_event_constructs() -> None:
    data = load_fixture('worker-event.json')
    event = WorkerEvent(
        task_id=data['task_id'],
        revision=data['revision'],
        event_type=data['event_type'],
        stage=data['stage'],
        emitted_at=parse_utc(data['emitted_at']),
        percent=data['percent'],
        payload=data['payload'],
        worker_event_id=data['worker_event_id'],
        sequence=data['sequence'],
    )
    assert event.event_type == 'progress'
    assert event.percent == 45
    assert event.sequence == 42
    assert event.payload['current_step'] == 3


def test_authorization_request_constructs() -> None:
    data = load_fixture('authorization-request.json')
    actor = ActorEvidence(
        actor_id=data['actor']['actor_id'],
        role=Role(data['actor']['role']),
        sources=tuple(data['actor']['sources']),
    )
    request = AuthorizationRequest(
        request_id=data['request_id'],
        actor=actor,
        session_id=data['session_id'],
        task_id=data['task_id'],
        intent=data['intent'],
        capability=data['capability'],
        object_scope=data['object_scope'],
        risk_scope=data['risk_scope'],
        within_intent_scope=data['within_intent_scope'],
        reversibility=Reversibility(data['reversibility']),
        provenance_summary=data['provenance_summary'],
        now=parse_utc(data['now']),
        action_fingerprint=data['action_fingerprint'],
    )
    assert request.actor.role is Role.OWNER
    assert request.reversibility is Reversibility.REVERSIBLE
    assert request.action_fingerprint == '7e3b9c1d5a4f2860e1b7c9d3a5f4e2b8c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5'


def test_memory_packet_constructs() -> None:
    data = load_fixture('memory-packet.json')
    packet = MemoryPacket(summaries=tuple(data['summaries']))
    assert packet.summaries == tuple(data['summaries'])
    assert len(packet.summaries) <= 8


def test_policy_decision_constructs() -> None:
    data = load_fixture('policy-decision.json')
    decision = PermissionDecision(
        outcome=PermissionOutcome(data['outcome']),
        reason=data['reason'],
        todo_id=data['todo_id'],
    )
    assert decision.outcome is PermissionOutcome.CONFIRMATION_REQUIRED
    assert decision.todo_id == 'todo-2041-9931'


def test_task_result_constructs() -> None:
    data = load_fixture('task-result.json')
    artifacts = tuple(ArtifactRef(**item) for item in data['artifacts'])
    evidence = tuple(
        Evidence(
            kind=item['kind'],
            summary=item['summary'],
            observed_at=parse_utc(item['observed_at']),
            artifacts=tuple(ArtifactRef(**ref) for ref in item['artifacts']),
            details=item['details'],
            evidence_id=item['evidence_id'],
        )
        for item in data['evidence']
    )
    result = TaskResult(
        result_id=data['result_id'],
        task_id=data['task_id'],
        revision=data['revision'],
        summary=data['summary'],
        artifacts=artifacts,
        evidence=evidence,
        error=None if data['error'] is None else StructuredError(**data['error']),
        key_steps=tuple(data['key_steps']),
        evidence_at=parse_utc(data['evidence_at']),
    )
    assert result.revision == 3
    assert result.artifacts[0].uri == 'file://reports/weekly-album-2026-W45.json'
    assert result.evidence[0].evidence_id == 'ev-2041-1'


def test_invalid_policy_decision_rejected() -> None:
    data = load_fixture('invalid', 'policy-decision-allow-with-todo.json')
    try:
        PermissionDecision(
            outcome=PermissionOutcome(data['outcome']),
            reason=data['reason'],
            todo_id=data['todo_id'],
        )
    except ValueError:
        pass
    else:
        raise AssertionError('allow decision with todo_id must be rejected')


def _main() -> None:
    """直接执行入口(参考 tests/ 的运行方式:python -X utf8 tests\test_*.py)。"""
    tests = [value for name, value in sorted(globals().items()) if name.startswith('test_') and callable(value)]
    for test in tests:
        test()
        print(f'PASS {test.__name__}')
    print(f'ALL PASS test_agent_runtime_contracts ({len(tests)} tests)')


if __name__ == '__main__':
    _main()
