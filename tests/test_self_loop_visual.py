from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.input.contracts import MediaRef
from guanjia.self_loop.visual import LocalVisualSignal, VisualCandidateGate


def _media_ref(char: str = 'a') -> MediaRef:
    return MediaRef(
        media_id='snapshot-1',
        sha256=char * 64,
        media_type='image',
        size_bytes=42,
    )


def test_visual_gate_ignores_unchanged_scene() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    gate = VisualCandidateGate()
    signal = LocalVisualSignal('living-room', _media_ref(), now, change_score=0.1)

    assert gate.request(signal) is None


def test_visual_gate_accepts_material_change_and_respects_scene_cooldown() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    gate = VisualCandidateGate()
    signal = LocalVisualSignal('living-room', _media_ref(), now, change_score=0.8)

    request = gate.request(signal)
    assert request is not None
    assert request.media_ref == signal.media_ref
    assert gate.request(signal) is None

    later = LocalVisualSignal(
        'living-room',
        _media_ref('b'),
        now + timedelta(minutes=10),
        change_score=0.8,
    )
    assert gate.request(later) is not None


def test_obvious_visual_change_bypasses_scene_cooldown() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    gate = VisualCandidateGate()
    gate.request(LocalVisualSignal('living-room', _media_ref(), now, change_score=0.8))

    obvious = LocalVisualSignal(
        'living-room',
        _media_ref('b'),
        now + timedelta(minutes=1),
        change_score=1.0,
    )
    request = gate.request(obvious)

    assert request is not None
    assert request.reason == 'obvious change'


def test_same_obvious_media_and_hard_debounce_do_not_repeat_requests() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    gate = VisualCandidateGate(obvious_debounce_seconds=5)
    first = LocalVisualSignal('living-room', _media_ref('a'), now, change_score=1.0)
    assert gate.request(first) is not None
    assert gate.request(LocalVisualSignal('living-room', _media_ref('a'), now + timedelta(seconds=1), change_score=1.0)) is None
    assert gate.request(LocalVisualSignal('living-room', _media_ref('b'), now + timedelta(seconds=2), change_score=1.0)) is None
    assert gate.request(LocalVisualSignal('living-room', _media_ref('c'), now + timedelta(seconds=5), change_score=1.0)) is not None


def test_out_of_order_signal_never_changes_scene_gate_state() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    gate = VisualCandidateGate()
    assert gate.request(LocalVisualSignal('living-room', _media_ref('a'), now + timedelta(seconds=10), change_score=0.8)) is not None

    old = LocalVisualSignal('living-room', _media_ref('b'), now, change_score=1.0)
    assert gate.request(old) is None

    assert gate.request(LocalVisualSignal('living-room', _media_ref('c'), now + timedelta(minutes=10, seconds=10), change_score=0.8)) is not None


def test_scene_state_is_expired_and_bounded() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    gate = VisualCandidateGate(scene_state_ttl=timedelta(seconds=10), max_scenes=3)
    for index, char in enumerate(('a', 'b', 'c', 'd', 'e')):
        gate.request(LocalVisualSignal(f'scene-{index}', _media_ref(char), now + timedelta(seconds=index), change_score=0.8))
    assert len(gate._scenes) == 3
    assert set(gate._scenes) == {'scene-2', 'scene-3', 'scene-4'}

    gate.request(LocalVisualSignal('fresh', _media_ref('f'), now + timedelta(seconds=20), change_score=0.8))
    assert set(gate._scenes) == {'fresh'}
