from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.memory.confidence import IdentityCalibrator
from guanjia.memory.conflicts import decide_update
from datetime import datetime, timezone

from guanjia.memory.contracts import MediaRef, MemoryCandidate, MemoryRecord, MemorySource, RetentionLevel


def test_user_correction_overrides_and_nearby_evidence_is_uncertain() -> None:
    current = MemoryRecord.from_candidate(
        MemoryCandidate('owner.city', 'Beijing', 'fact', MemorySource.USER, 0.9, 8, RetentionLevel.PERMANENT)
    )
    corrected = MemoryCandidate(
        'owner.city', 'Shanghai', 'fact', MemorySource.USER_CORRECTION, 0.1, 8, RetentionLevel.PERMANENT
    )
    assert decide_update(current, corrected, IdentityCalibrator()).record.content == 'Shanghai'

    nearby = MemoryCandidate('owner.city', 'Guangzhou', 'fact', MemorySource.OBSERVATION, 0.92, 8)
    decision = decide_update(current, nearby, IdentityCalibrator(), margin=0.05)
    assert decision.record is current
    assert decision.conflict is not None and decision.conflict.status == 'uncertain'


def test_memory_dtos_freeze_metadata_and_reject_invalid_scores() -> None:
    metadata = {'label': 'kitchen'}
    media = MediaRef('m1', 'image', 'sha256:x', metadata)
    metadata['label'] = 'changed'
    assert media.metadata['label'] == 'kitchen'
    try:
        MemoryCandidate('key', 'text', 'fact', MemorySource.USER, 1.1, 1)
    except ValueError:
        return
    raise AssertionError('out-of-range confidence accepted')
