"""Score calibration and cautious replacement decisions for input processing."""

from __future__ import annotations

from collections.abc import Callable


Calibrator = Callable[[float], float]


class CalibrationRegistry:
    """Named calibrators convert processor-local scores into one common scale."""

    def __init__(self) -> None:
        self._calibrators: dict[str, Calibrator] = {}

    def register(self, name: str, calibrator: Calibrator) -> None:
        if not name:
            raise ValueError('calibrator name must be non-empty')
        if not callable(calibrator):
            raise TypeError('calibrator must be callable')
        self._calibrators[name] = calibrator

    def calibrate(self, name: str | None, raw_score: float) -> float:
        if not 0 <= raw_score <= 1:
            raise ValueError('raw_score must be between 0 and 1')
        if name is not None and name not in self._calibrators:
            raise ValueError(f'calibrator is not registered: {name}')
        calibrated = raw_score if name is None else self._calibrators[name](raw_score)
        if not isinstance(calibrated, (int, float)) or isinstance(calibrated, bool) or not 0 <= calibrated <= 1:
            raise ValueError('calibrator must return a score between 0 and 1')
        return float(calibrated)


def replacement_decision(
    current_confidence: float | None,
    candidate_confidence: float,
    *,
    margin: float,
) -> tuple[bool, bool]:
    """Return ``(replace, review_required)`` without silently overriding close scores."""
    if margin < 0 or margin > 1:
        raise ValueError('margin must be between 0 and 1')
    if not 0 <= candidate_confidence <= 1:
        raise ValueError('candidate_confidence must be between 0 and 1')
    if current_confidence is None:
        return True, False
    if not 0 <= current_confidence <= 1:
        raise ValueError('current_confidence must be between 0 and 1')
    difference = candidate_confidence - current_confidence
    if difference >= margin:
        return True, False
    return False, abs(difference) <= margin
