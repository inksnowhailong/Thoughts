"""Local-only visual gating for self-loop description requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

from guanjia.input.contracts import MediaRef


@dataclass(frozen=True)
class LocalVisualSignal:
    """Scores produced locally from an opaque camera snapshot reference."""

    scene_id: str
    media_ref: MediaRef
    observed_at: datetime
    change_score: float = 0.0
    person_score: float = 0.0
    target_score: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.scene_id, str) or not self.scene_id.strip():
            raise ValueError('scene_id must be a non-empty string')
        if not isinstance(self.media_ref, MediaRef):
            raise ValueError('media_ref must be a MediaRef')
        for name in ('change_score', 'person_score', 'target_score'):
            score = getattr(self, name)
            if isinstance(score, bool) or type(score) not in (int, float) or not 0.0 <= score <= 1.0:
                raise ValueError(f'{name} must be a number between 0 and 1')
        object.__setattr__(self, 'observed_at', _utc(self.observed_at, 'observed_at'))


@dataclass(frozen=True)
class VisualDescriptionRequest:
    """An opaque media-reference request for a separately injected visual service."""

    scene_id: str
    media_ref: MediaRef
    observed_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.scene_id, str) or not self.scene_id.strip():
            raise ValueError('scene_id must be a non-empty string')
        if not isinstance(self.media_ref, MediaRef):
            raise ValueError('media_ref must be a MediaRef')
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError('reason must be a non-empty string')
        object.__setattr__(self, 'observed_at', _utc(self.observed_at, 'observed_at'))


@dataclass(frozen=True)
class VisualDescriptionResult:
    """A description result that preserves its opaque media provenance."""

    request: VisualDescriptionRequest
    description: str
    described_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.request, VisualDescriptionRequest):
            raise ValueError('request must be a VisualDescriptionRequest')
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError('description must be a non-empty string')
        object.__setattr__(self, 'described_at', _utc(self.described_at, 'described_at'))


class VisualCandidateGate:
    """Issue requests only for locally meaningful signals; never inspect media bytes."""

    def __init__(
        self,
        *,
        change_threshold: float = 0.6,
        person_threshold: float = 0.7,
        target_threshold: float = 0.7,
        obvious_change_threshold: float = 0.95,
        scene_cooldown_seconds: int = 600,
        obvious_debounce_seconds: int = 5,
        scene_state_ttl: timedelta = timedelta(hours=1),
        max_scenes: int = 256,
    ) -> None:
        for value, name in (
            (change_threshold, 'change_threshold'),
            (person_threshold, 'person_threshold'),
            (target_threshold, 'target_threshold'),
            (obvious_change_threshold, 'obvious_change_threshold'),
        ):
            _score(value, name)
        if type(scene_cooldown_seconds) is not int or scene_cooldown_seconds <= 0:
            raise ValueError('scene_cooldown_seconds must be a positive integer')
        if type(obvious_debounce_seconds) is not int or obvious_debounce_seconds <= 0:
            raise ValueError('obvious_debounce_seconds must be a positive integer')
        if not isinstance(scene_state_ttl, timedelta) or scene_state_ttl <= timedelta(0):
            raise ValueError('scene_state_ttl must be a positive timedelta')
        if type(max_scenes) is not int or max_scenes <= 0:
            raise ValueError('max_scenes must be a positive integer')
        if obvious_change_threshold < change_threshold:
            raise ValueError('obvious_change_threshold must be at least change_threshold')
        self.change_threshold = change_threshold
        self.person_threshold = person_threshold
        self.target_threshold = target_threshold
        self.obvious_change_threshold = obvious_change_threshold
        self.scene_cooldown = timedelta(seconds=scene_cooldown_seconds)
        self.obvious_debounce = timedelta(seconds=obvious_debounce_seconds)
        self.scene_state_ttl = scene_state_ttl
        self.max_scenes = max_scenes
        self._scenes: dict[str, _SceneGateState] = {}
        self._lock = RLock()

    def request(self, signal: LocalVisualSignal) -> VisualDescriptionRequest | None:
        """Return a request when local scores justify it and record scene cooldown."""
        if not isinstance(signal, LocalVisualSignal):
            raise ValueError('signal must be a LocalVisualSignal')
        with self._lock:
            self._prune_expired(signal.observed_at)
            previous = self._scenes.get(signal.scene_id)
            if previous is not None and signal.observed_at <= previous.last_observed_at:
                return None
            if previous is not None and signal.media_ref.sha256 == previous.last_media_hash:
                self._store(signal.scene_id, _SceneGateState(
                    signal.observed_at,
                    previous.last_request_at,
                    previous.last_media_hash,
                ))
                return None
            reason = self._reason(signal)
            if reason is None:
                self._store(signal.scene_id, _SceneGateState(
                    signal.observed_at,
                    previous.last_request_at if previous is not None else None,
                    signal.media_ref.sha256,
                ))
                return None
            last_request_at = previous.last_request_at if previous is not None else None
            if last_request_at is not None:
                elapsed = signal.observed_at - last_request_at
                if reason == 'obvious change' and elapsed < self.obvious_debounce:
                    self._store(signal.scene_id, _SceneGateState(
                        signal.observed_at,
                        last_request_at,
                        signal.media_ref.sha256,
                    ))
                    return None
                if reason != 'obvious change' and elapsed < self.scene_cooldown:
                    self._store(signal.scene_id, _SceneGateState(
                        signal.observed_at,
                        last_request_at,
                        signal.media_ref.sha256,
                    ))
                    return None
            self._store(signal.scene_id, _SceneGateState(
                signal.observed_at,
                signal.observed_at,
                signal.media_ref.sha256,
            ))
            return VisualDescriptionRequest(signal.scene_id, signal.media_ref, signal.observed_at, reason)

    def _reason(self, signal: LocalVisualSignal) -> str | None:
        if signal.change_score >= self.obvious_change_threshold:
            return 'obvious change'
        if signal.change_score >= self.change_threshold:
            return 'local change'
        if signal.person_score >= self.person_threshold:
            return 'person detected'
        if signal.target_score >= self.target_threshold:
            return 'target detected'
        return None

    def _store(self, scene_id: str, state: _SceneGateState) -> None:
        if scene_id not in self._scenes and len(self._scenes) >= self.max_scenes:
            oldest_scene_id = min(
                self._scenes,
                key=lambda item: self._scenes[item].last_observed_at,
            )
            del self._scenes[oldest_scene_id]
        self._scenes[scene_id] = state

    def _prune_expired(self, observed_at: datetime) -> None:
        cutoff = observed_at - self.scene_state_ttl
        expired = [
            scene_id
            for scene_id, state in self._scenes.items()
            if state.last_observed_at < cutoff
        ]
        for scene_id in expired:
            del self._scenes[scene_id]


@dataclass(frozen=True)
class _SceneGateState:
    last_observed_at: datetime
    last_request_at: datetime | None
    last_media_hash: str


def _score(value: float, name: str) -> None:
    if isinstance(value, bool) or type(value) not in (int, float) or not 0.0 <= value <= 1.0:
        raise ValueError(f'{name} must be a number between 0 and 1')


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{name} must be an aware UTC datetime')
    return value.astimezone(timezone.utc)
