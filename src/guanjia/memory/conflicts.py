"""Deterministic current-value conflict policy; no record history is retained."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .confidence import ConfidenceCalibrator
from .contracts import ConflictCase, MemoryCandidate, MemoryRecord, MemorySource


@dataclass(frozen=True)
class UpdateDecision:
    record: MemoryRecord
    conflict: ConflictCase | None = None


def decide_update(
    current: MemoryRecord,
    incoming: MemoryCandidate,
    calibrator: ConfidenceCalibrator,
    *,
    margin: float = 0.1,
) -> UpdateDecision:
    """Return an overwrite only when an incoming value is decisively stronger."""
    if incoming.key != current.key:
        raise ValueError('conflicts can only be decided for the same memory key')
    if incoming.source is MemorySource.USER_CORRECTION:
        return UpdateDecision(current.replace_with(incoming))

    new_confidence = calibrator.calibrate(incoming.confidence, incoming.source)
    if new_confidence >= current.confidence + margin:
        return UpdateDecision(current.replace_with(incoming))
    if abs(new_confidence - current.confidence) < margin:
        return UpdateDecision(current, ConflictCase(str(uuid4()), current.key, current, incoming))
    return UpdateDecision(current)
