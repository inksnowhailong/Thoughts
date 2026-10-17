from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.core.agent_runtime import InMemoryCommitPort, MainAgentRuntime
from guanjia.core.main_event_parser import EventValidationError, parse_jsonl


def event(kind: str, event_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    if kind == 'turn.completed' and payload is None:
        payload = {'turn_id': 'turn-id-1'}
    return {
        'version': '1.0',
        'event_id': event_id,
        'correlation_id': 'turn-1',
        'idempotency_key': event_id,
        'type': kind,
        'payload': payload or {},
    }


def test_parser_requires_strict_envelope_and_jsonl() -> None:
    batch = '\n'.join(json.dumps(item) for item in (
        event('reply.delta', '1', {'text': 'hello'}),
        event('turn.completed', '2'),
    ))
    parsed = parse_jsonl(batch)
    assert [item.type for item in parsed] == ['reply.delta', 'turn.completed']

    bad = event('reply.delta', '1', {'text': 'hello'})
    del bad['idempotency_key']
    try:
        parse_jsonl(json.dumps(bad))
    except EventValidationError:
        pass
    else:
        raise AssertionError('missing idempotency key was accepted')


def test_valid_turn_commits_once_after_staging_all_effects() -> None:
    commits = InMemoryCommitPort()
    runtime = MainAgentRuntime(commit=commits)
    outcome = runtime.process_jsonl('\n'.join(json.dumps(item) for item in (
        event('reply.delta', '1', {'text': 'hello'}),
        event('tool.read', '2', {'resource': 'file:///note.txt'}),
        event('output.request', '3', {'channel': 'panel', 'text': 'hello'}),
        event('turn.completed', '4'),
    )))

    assert outcome.committed and not outcome.replayed
    assert [effect.kind for effect in commits.commits[0]] == [
        'reply.delta', 'tool.read', 'output.request', 'session.write',
    ]


def test_batch_supports_every_declared_main_event_type() -> None:
    commits = InMemoryCommitPort()
    runtime = MainAgentRuntime(commit=commits)
    runtime.process_events((
        event('reply.delta', '1', {'text': 'thinking'}),
        event('task.create', '3', {'task_id': 't', 'revision': 1, 'goal': 'work'}),
        event('task.control', '4', {'task_id': 't', 'action': 'pause'}),
        event('tool.read', '5', {'resource': 'file:///brief'}),
        event('output.request', '6', {'channel': 'panel', 'text': 'queued'}),
        event('memory.request', '7', {'query': 'facts'}),
        event('turn.completed', '8'),
    ))
    assert [effect.kind for effect in commits.commits[0][:-1]] == [
        'reply.delta', 'task.create', 'task.control',
        'tool.read', 'output.request', 'memory.request',
    ]


def test_invalid_batch_never_leaks_partial_effects_and_repair_runs_once() -> None:
    commits = InMemoryCommitPort()
    repaired: list[object] = []

    def repairer(_: object) -> str:
        repaired.append(True)
        return '\n'.join(json.dumps(item) for item in (
            event('reply.silent', '1'),
            event('turn.completed', '2'),
        ))

    runtime = MainAgentRuntime(commit=commits, repairer=repairer)
    outcome = runtime.process_jsonl('\n'.join(json.dumps(item) for item in (
        event('reply.delta', '1', {'text': 'first'}),
        event('not.allowed', '2'),
    )))
    assert outcome.committed and outcome.repaired
    assert len(repaired) == len(commits.commits) == 1

    rejected = MainAgentRuntime(commit=commits)
    try:
        rejected.process_events((event('reply.delta', '3', {'text': 'partial'}),))
    except EventValidationError:
        pass
    else:
        raise AssertionError('unterminated turn was accepted')
    assert len(commits.commits) == 1

    attempts: list[object] = []
    never_fixed = MainAgentRuntime(
        commit=commits,
        repairer=lambda source: attempts.append(source) or source,
    )
    try:
        never_fixed.process_events((event('not.allowed', 'bad'),))
    except EventValidationError:
        pass
    else:
        raise AssertionError('unrepairable batch was accepted')
    assert len(attempts) == 1


def test_duplicate_turn_replays_without_second_commit_and_shadow_has_no_effects() -> None:
    commits = InMemoryCommitPort()
    source = '\n'.join(json.dumps(item) for item in (
        event('reply.delta', '0', {'text': 'queued'}),
        event('task.create', '1', {'task_id': 't-1', 'revision': 1, 'goal': 'research'}),
        event('memory.request', '2', {'query': 'context'}),
        event('turn.completed', '3'),
    ))
    runtime = MainAgentRuntime(commit=commits)
    assert runtime.process_jsonl(source).committed
    assert runtime.process_jsonl(source).replayed
    assert len(commits.commits) == 1

    shadow = MainAgentRuntime(commit=commits, shadow=True)
    outcome = shadow.process_jsonl(source.replace('turn-1', 'turn-shadow'))
    assert outcome.shadow and not outcome.committed
    assert len(commits.commits) == 1


def test_deep_frozen_payload_grammar_and_concurrent_durable_idempotency() -> None:
    payload = {'text': ['not', 'a', 'string']}
    try:
        parse_jsonl(json.dumps(event('reply.delta', '1', payload)))
    except EventValidationError:
        pass
    else:
        raise AssertionError('invalid exact payload schema was accepted')
    silent = event('reply.silent', '1')
    delta = event('reply.delta', '2', {'text': 'x'})
    try:
        MainAgentRuntime(commit=InMemoryCommitPort()).process_events((silent, delta, event('turn.completed', '3')))
    except EventValidationError:
        pass
    else:
        raise AssertionError('reply grammar was accepted')

    port = InMemoryCommitPort()
    source = '\n'.join(json.dumps(item) for item in (event('reply.delta', '1', {'text': 'x'}), event('turn.completed', '2')))
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: MainAgentRuntime(commit=port).process_jsonl(source), range(4)))
    assert sum(not item.replayed for item in outcomes) == len(port.commits) == 1
    changed = source.replace('"x"', '"y"')
    try:
        MainAgentRuntime(commit=port).process_jsonl(changed)
    except Exception:
        pass
    else:
        raise AssertionError('same idempotency key with changed event was accepted')


def test_commit_failure_does_not_trigger_repair_or_partial_retry() -> None:
    class FailingPort:
        def __init__(self) -> None:
            self.calls = 0

        def commit_if_absent(self, turn_id: str, fingerprint: str, effects: tuple[object, ...]) -> object:
            self.calls += 1
            raise RuntimeError('transaction failed before commit')

    repairs: list[object] = []
    port = FailingPort()
    runtime = MainAgentRuntime(commit=port, repairer=lambda source: repairs.append(source) or source)
    source = '\n'.join(json.dumps(item) for item in (
        event('reply.delta', '1', {'text': 'x'}), event('turn.completed', '2'),
    ))
    try:
        runtime.process_jsonl(source)
    except RuntimeError:
        pass
    else:
        raise AssertionError('commit error was hidden')
    assert port.calls == 1 and not repairs
