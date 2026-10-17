from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.events import NullHub
from guanjia.panel import PanelBackend
from guanjia.settings import SettingsStore


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_shadow_projection_is_an_exact_allowlist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        store = SettingsStore(tmp_dir / 'settings.json')
        panel = PanelBackend(
            NullHub(),
            store,
            status=lambda: {},
            control=lambda action: (True, action),
            inject=lambda text, dry_run: {},
            levels_path=tmp_dir / 'levels.json',
            audit_dir=tmp_dir / 'audit',
        )
        panel.attach_shadow_projection(lambda: {
            'flags': {
                'input_shadow_v1': True,
                'core_shadow_v1': False,
                'permission_shadow_v1': True,
                'raw_value': 'SECRET_ENV_VALUE',
            },
            'input': {
                'queue': 2,
                'accepted': 15,
                'dropped': 1,
                'failures': 0,
                'difference_fields': ['channel', 'secret_field_name', 'C:/private/audio.wav'],
                'media_path': 'C:/private/audio.wav',
                'session_key': 'agent:main:main',
            },
            'core': {
                'queue': 1,
                'accepted': 8,
                'dropped': 2,
                'failures': 1,
                'difference_fields': ['reply_present', 'task_create_count', 'Bearer sk-secret123456'],
                'goal': '敏感任务目标',
                'params': {'command': 'del /f /q secret.txt'},
                'approval_id': 'approval-secret-id',
                'token': 'sk-secret123456',
            },
            'extra_secret': 'should_never_appear',
        })

        code, payload = panel.route_get('/api/architecture-shadow', {}) or (500, {})
        check(code == 200 and payload.get('ok') is True, 'GET /api/architecture-shadow returns 200 ok')
        check(
            payload['shadow'] == {
                'flags': {
                    'input_shadow_v1': True,
                    'core_shadow_v1': False,
                },
                'input': {
                    'queue': 2,
                    'accepted': 15,
                    'dropped': 1,
                    'failures': 0,
                    'difference_fields': ['channel'],
                },
                'core': {
                    'queue': 1,
                    'accepted': 8,
                    'dropped': 2,
                    'failures': 1,
                    'difference_fields': ['reply_present', 'task_create_count'],
                },
            },
            'shadow projection matches exact allowlist schema',
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            'SECRET_ENV_VALUE',
            'C:/private/audio.wav',
            'agent:main:main',
            '敏感任务目标',
            'del /f /q',
            'approval-secret-id',
            'sk-secret123456',
            'should_never_appear',
        ):
            check(forbidden not in serialized, f'forbidden value leaked: {forbidden}')


def test_shadow_projection_is_get_only_and_html_has_no_action_buttons() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        store = SettingsStore(tmp_dir / 'settings.json')
        panel = PanelBackend(
            NullHub(),
            store,
            status=lambda: {},
            control=lambda action: (True, action),
            inject=lambda text, dry_run: {},
            levels_path=tmp_dir / 'levels.json',
            audit_dir=tmp_dir / 'audit',
        )
        check(panel.route_post('/api/architecture-shadow', {'action': 'execute'}) is None, 'POST is not registered for architecture shadow')
        html = panel.html().decode('utf-8')
        start = html.index('<details id="shadowDetails"')
        end = html.index('</details>', start) + len('</details>')
        block = html[start:end]
        check('<button' not in block and '<form' not in block, 'architecture shadow HTML block has no action buttons or forms')


if __name__ == '__main__':
    test_shadow_projection_is_an_exact_allowlist()
    test_shadow_projection_is_get_only_and_html_has_no_action_buttons()
    print('ALL PASS test_panel_shadow_projection')
