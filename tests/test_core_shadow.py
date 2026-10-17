from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.core.main_program import (
    CancellationToken,
    FORBIDDEN_SESSION_KEYS,
    MainProgram,
    ShadowTurnRequest,
)
from guanjia.core.shadow import CoreShadowCoordinator
from guanjia.input.adapters.contracts import ShadowInputRecord


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_jsonl(correlation_id: str, text: str = '影子回复', task_goal: str | None = None) -> str:
    events = [
        {
            'version': '1.0',
            'event_id': f'{correlation_id}-e1',
            'correlation_id': correlation_id,
            'idempotency_key': f'{correlation_id}-k1',
            'type': 'reply.delta',
            'payload': {'text': text},
        },
    ]
    if task_goal is not None:
        events.append({
            'version': '1.0',
            'event_id': f'{correlation_id}-e2',
            'correlation_id': correlation_id,
            'idempotency_key': f'{correlation_id}-k2',
            'type': 'task.create',
            'payload': {'task_id': 'task-shadow-1', 'revision': 1, 'goal': task_goal},
        })
    events.append({
        'version': '1.0',
        'event_id': f'{correlation_id}-done',
        'correlation_id': correlation_id,
        'idempotency_key': f'{correlation_id}-kdone',
        'type': 'turn.completed',
        'payload': {'turn_id': f'turn-{correlation_id}'},
    })
    return '\n'.join(json.dumps(item) for item in events)


def test_main_program_shadow_never_commits_or_reuses_main_session() -> None:
    seen_requests: list[ShadowTurnRequest] = []

    class RecordingGenerator:
        def generate(self, request: ShadowTurnRequest, cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            seen_requests.append(request)
            return make_jsonl(request.correlation_id, task_goal='只做提案不落库')

    program = MainProgram(generator=RecordingGenerator())
    first = program.run_shadow_turn(input_id='in-1', correlation_id='corr-1', text='第一轮完整输入')
    second = program.run_shadow_turn(input_id='in-2', correlation_id='corr-2', text='第二轮完整输入')

    check(first.shadow and first.committed is None and first.effects == (), 'shadow turn must never commit effects')
    check(any(effect.kind == 'task.create' for effect in first.proposals), 'shadow turn retains proposals for comparison only')
    check(all(effect.kind != 'session.write' for effect in first.proposals), 'shadow proposals exclude session.write')
    check(len(seen_requests) == 2, 'generator called for each complete turn')
    check(seen_requests[0].session_key != seen_requests[1].session_key, 'each shadow turn uses a distinct session key')
    for req in seen_requests:
        check(req.session_key.startswith('agent:shadow:'), 'shadow session key uses isolated namespace')
        check(req.session_key not in FORBIDDEN_SESSION_KEYS, 'production main session is never reused')
        check(req.tools_disabled is True, 'shadow turn enforces tools_disabled=True')
        check(req.model == 'glm-4-flash', 'ModelPolicy selects free-quota model first')


def test_coordinator_cancels_inflight_turn_on_new_input_or_barge_in() -> None:
    first_started = threading.Event()
    first_cancelled = threading.Event()
    release_first = threading.Event()
    outcomes: list[tuple[str, str]] = []

    class BlockingGenerator:
        def generate(self, request: ShadowTurnRequest, cancellation: CancellationToken) -> str:
            if request.input_id == 'input-1':
                cancellation.on_cancel(first_cancelled.set)
                first_started.set()
                release_first.wait(1.0)
                return make_jsonl(request.correlation_id, text='过期输出')
            return make_jsonl(request.correlation_id, text='最新输出')

    coordinator = CoreShadowCoordinator(
        generator=BlockingGenerator(),
        on_outcome=lambda record, outcome: outcomes.append((str(record.input_id), outcome.correlation_id)),
    )
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    rec1 = ShadowInputRecord('input-1', 'corr-1', 'voice', '第一句完整语音', now, {})
    rec2 = ShadowInputRecord('input-2', 'corr-2', 'voice', '第二句打断语音', now, {})

    check(coordinator.observe(rec1), 'first complete turn accepted')
    check(first_started.wait(1.0), 'first turn entered generator')
    check(coordinator.observe(rec2), 'second complete turn supersedes first')
    check(first_cancelled.wait(1.0), 'new input immediately cancels in-flight shadow turn')
    release_first.set()
    check(coordinator.wait_idle(timeout=1.0), 'coordinator drains latest turn')

    metrics = coordinator.metrics()
    check(outcomes == [('input-2', 'corr-2')], 'stale first turn outcome is discarded')
    check(metrics.accepted == 2 and metrics.completed == 1 and metrics.cancelled >= 1, 'metrics track cancellation and completion')
    diffs = coordinator.record_difference_fields(['reply_intent', 'secret_token', 'task_create_count'])
    check(diffs == ('reply_intent', 'task_create_count'), 'only allowlisted difference fields are retained')
    check(coordinator.close(timeout=0.5), 'coordinator closes cleanly')


if __name__ == '__main__':
    test_main_program_shadow_never_commits_or_reuses_main_session()
    test_coordinator_cancels_inflight_turn_on_new_input_or_barge_in()
    print('ALL PASS test_core_shadow')
