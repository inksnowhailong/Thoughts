from __future__ import annotations

import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.core.openclaw_adapter import OpenClawAdapter, SQLiteSessionMapStore


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def invoke(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        return {'ok': True, 'request_id': 'oc-1'}


def test_adapter_maps_logical_sessions_and_only_forwards_protocol() -> None:
    transport = RecordingTransport()
    state: dict[str, str] = {}
    adapter = OpenClawAdapter(transport, state=state, namespace='test')

    first = adapter.session_for('task/task-1')
    assert first == adapter.session_for('task/task-1')
    response = adapter.call(
        logical_session='task/task-1',
        model='chosen-by-policy',
        command='start',
        messages=({'role': 'user', 'content': 'work'},),
    )
    assert response['ok'] is True
    assert transport.requests == [{
        'session': first,
        'model': 'chosen-by-policy',
        'command': 'start',
        'messages': [{'role': 'user', 'content': 'work'}],
    }]


def test_adapter_recovers_mapping_across_restart_and_rejects_unsafe_logical_keys() -> None:
    state: dict[str, str] = {}
    before = OpenClawAdapter(RecordingTransport(), state=state, namespace='test').session_for('main/user-1')
    after = OpenClawAdapter(RecordingTransport(), state=state, namespace='test').session_for('main/user-1')
    assert before == after

    adapter = OpenClawAdapter(RecordingTransport(), state=state)
    for unsafe in ('', '../main', 'main//user', 'main\\user', '/main'):
        try:
            adapter.session_for(unsafe)
        except ValueError:
            continue
        raise AssertionError(f'unsafe logical session accepted: {unsafe!r}')


def test_sqlite_session_mapping_is_atomic_across_instances_and_close_is_explicit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / 'sessions.sqlite')
        first = SQLiteSessionMapStore(path)
        second = SQLiteSessionMapStore(path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            sessions = list(pool.map(
                lambda store: OpenClawAdapter(RecordingTransport(), state=store, namespace='test').session_for('task/task-1'),
                (first, second),
            ))
        assert sessions[0] == sessions[1]
        first.close()
        try:
            first.get_or_create('test:task/other', 'oc:test:other')
        except RuntimeError:
            pass
        else:
            raise AssertionError('closed store accepted a query')
        second.close()
