"""Deterministic model selection independent of business workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping


_ROLES = frozenset({'main', 'worker', 'vision', 'memory'})


@dataclass(frozen=True)
class ModelProfile:
    """Capabilities and economic properties supplied by deployment config."""

    name: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    cost: int
    free_tokens: int = 0
    free_tokens_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('model name must be non-empty')
        if type(self.roles) is not tuple or not self.roles or not all(type(item) is str for item in self.roles):
            raise ValueError('model roles must be a non-empty string tuple')
        if not set(self.roles).issubset(_ROLES) or len(set(self.roles)) != len(self.roles):
            raise ValueError('model roles must be supported and non-empty')
        if type(self.capabilities) is not tuple or not all(type(item) is str and item for item in self.capabilities):
            raise ValueError('model capabilities must be a string tuple')
        if type(self.cost) is not int or self.cost < 0:
            raise ValueError('model cost must be a non-negative integer')
        if type(self.free_tokens) is not int or self.free_tokens < 0:
            raise ValueError('free_tokens must be a non-negative integer')
        if self.free_tokens_expires_at is not None:
            if (
                not isinstance(self.free_tokens_expires_at, datetime)
                or self.free_tokens_expires_at.tzinfo is None
                or self.free_tokens_expires_at.utcoffset() != timedelta(0)
            ):
                raise ValueError('free token expiry must be an aware UTC datetime')


@dataclass(frozen=True)
class QuotaSnapshot:
    """Observed free quota; policy reads it but never mutates it."""

    free_tokens: int
    expires_at: datetime | None
    source: str

    def __post_init__(self) -> None:
        if type(self.free_tokens) is not int or self.free_tokens < 0 or not isinstance(self.source, str) or not self.source:
            raise ValueError('quota snapshot is invalid')
        if self.expires_at is not None and (
            not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() != timedelta(0)
        ):
            raise ValueError('quota expiry must be aware UTC')


@dataclass(frozen=True)
class ModelSelection:
    """Selection plus auditable criterion; no quota deduction is implied."""

    profile: ModelProfile
    reason: str
    quota_source: str

    @property
    def name(self) -> str:
        return self.profile.name


class ModelPolicy:
    """Ranks configuration-provided models by free capacity, cost, then name."""

    def __init__(self, profiles: tuple[ModelProfile, ...]) -> None:
        if not profiles:
            raise ValueError('at least one model profile is required')
        if len({profile.name for profile in profiles}) != len(profiles):
            raise ValueError('model profile names must be unique')
        self._profiles = profiles

    def select(
        self,
        role: str,
        *,
        required_capabilities: tuple[str, ...] = (),
        failed_models: tuple[str, ...] = (),
        estimated_tokens: int = 0,
        quota: Mapping[str, QuotaSnapshot] | None = None,
        now: datetime | None = None,
    ) -> ModelSelection:
        """Return the same eligible configuration profile for equal inputs."""
        if role not in _ROLES:
            raise LookupError(f'unsupported model role: {role}')
        if type(required_capabilities) is not tuple or type(failed_models) is not tuple:
            raise TypeError('capabilities and failed models must be tuples')
        if not all(type(item) is str for item in required_capabilities + failed_models):
            raise TypeError('capabilities and failed models must contain strings')
        if type(estimated_tokens) is not int or estimated_tokens < 0:
            raise ValueError('estimated_tokens must be non-negative')
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() != timedelta(0):
            raise ValueError('now must be aware UTC')
        required = set(required_capabilities)
        failed = set(failed_models)
        candidates = [
            profile for profile in self._profiles
            if role in profile.roles
            and required.issubset(profile.capabilities)
            and profile.name not in failed
        ]
        if not candidates:
            raise LookupError(f'no eligible model for {role}')
        def quota_for(profile: ModelProfile) -> tuple[int, datetime | None, str]:
            if quota is not None:
                observed = quota.get(profile.name)
                if observed is None:
                    return 0, None, 'absent'
                return observed.free_tokens, observed.expires_at, observed.source
            return profile.free_tokens, profile.free_tokens_expires_at, 'profile'

        free = [
            (profile, quota_for(profile))
            for profile in candidates
            if estimated_tokens > 0 and quota_for(profile)[0] > 0 and quota_for(profile)[0] >= estimated_tokens
            and (quota_for(profile)[1] is None or quota_for(profile)[1] > current)
        ]
        if free:
            profile, observed = min(free, key=lambda item: (-item[1][0], item[0].cost, item[0].name))
            return ModelSelection(profile, 'eligible free quota', observed[2])
        profile = min(candidates, key=lambda item: (item.cost, item.name))
        return ModelSelection(profile, 'lowest eligible cost after free quota', 'none')
