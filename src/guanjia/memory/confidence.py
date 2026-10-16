"""Confidence normalization contracts; callers inject domain calibrators."""

from __future__ import annotations

from typing import Protocol

from .contracts import MemorySource


class ConfidenceCalibrator(Protocol):
    def calibrate(self, raw_score: float, source: MemorySource) -> float:
        """Convert a source-local score to the shared 0..1 confidence scale."""


class IdentityCalibrator:
    """Default pass-through calibrator for already-normalized domain inputs."""

    def calibrate(self, raw_score: float, source: MemorySource) -> float:
        if not 0.0 <= raw_score <= 1.0:
            raise ValueError('raw_score must be between 0 and 1')
        return raw_score
