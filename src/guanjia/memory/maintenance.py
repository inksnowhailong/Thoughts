"""Pure maintenance planning for the backend-independent memory domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re

from .contracts import MemoryRecord, RetentionLevel


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{name} must be an aware UTC datetime')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class MaintenanceBudget:
    """Policy limits used by one deterministic planning pass."""

    max_records: int
    temporary_ttl: timedelta = timedelta(days=30)
    index_stale_after: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if type(self.max_records) is not int or self.max_records < 1:
            raise ValueError('max_records must be a positive integer')
        if self.temporary_ttl <= timedelta(0) or self.index_stale_after <= timedelta(0):
            raise ValueError('maintenance durations must be positive')


@dataclass(frozen=True)
class MaintenanceAction:
    """One proposed operation; callers decide whether and how to apply it."""

    kind: str
    record_ids: tuple[str, ...] = ()
    target_record_id: str | None = None
    retention: RetentionLevel | None = None
    reason: str = ''

    def __post_init__(self) -> None:
        if self.kind not in {'merge', 'adjust_retention', 'expire', 'rebuild_index'}:
            raise ValueError('invalid maintenance action')
        if not self.reason:
            raise ValueError('reason must be non-empty')
        object.__setattr__(self, 'record_ids', tuple(self.record_ids))


@dataclass(frozen=True)
class MaintenancePlan:
    """An immutable proposal with no repository or workspace side effects."""

    actions: tuple[MaintenanceAction, ...] = ()
    budget_before: int = 0
    budget_after: int = 0
    rejections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.budget_before < 0 or self.budget_after < 0:
            raise ValueError('budgets cannot be negative')
        object.__setattr__(self, 'actions', tuple(self.actions))
        object.__setattr__(self, 'rejections', tuple(self.rejections))


class MaintenancePlanner:
    """Generate maintenance proposals without reading or writing a repository."""

    def __init__(self, budget: MaintenanceBudget) -> None:
        self._budget = budget

    def plan(
        self,
        records: tuple[MemoryRecord, ...],
        *,
        now: datetime,
        index_built_at: datetime | None = None,
    ) -> MaintenancePlan:
        now = _utc(now, 'now')
        if index_built_at is not None:
            index_built_at = _utc(index_built_at, 'index_built_at')
        records = tuple(records)
        if not all(isinstance(record, MemoryRecord) for record in records):
            raise ValueError('records must contain MemoryRecord values')

        actions: list[MaintenanceAction] = []
        removed_ids: set[str] = set()
        for duplicate_group in self._duplicate_groups(records):
            keeper = max(
                duplicate_group,
                key=lambda record: (
                    record.importance,
                    record.use_count,
                    record.last_used_at or record.updated_at,
                    record.id,
                ),
            )
            actions.append(
                MaintenanceAction(
                    'merge',
                    tuple(record.id for record in duplicate_group),
                    target_record_id=keeper.id,
                    reason='equivalent memory content',
                )
            )
            removed_ids.update(record.id for record in duplicate_group if record.id != keeper.id)

        for record in records:
            if record.id in removed_ids:
                continue
            if record.retention is RetentionLevel.ADAPTIVE and (
                record.importance >= 8 or record.evidence or record.use_count >= 2
            ):
                actions.append(
                    MaintenanceAction(
                        'adjust_retention',
                        (record.id,),
                        retention=RetentionLevel.LONG_TERM,
                        reason='importance, evidence, or repeated use warrants long-term retention',
                    )
                )
            if self._is_expired_temporary(record, now):
                actions.append(
                    MaintenanceAction(
                        'expire',
                        (record.id,),
                        reason='temporary retention expired without evidence',
                    )
                )
                removed_ids.add(record.id)

        budget_after = len(records) - len(removed_ids)
        rejections: list[str] = []
        if budget_after > self._budget.max_records:
            rejections.append('budget exceeded; no safe expiry candidate')
        if self._needs_reindex(actions, now, index_built_at):
            actions.append(
                MaintenanceAction(
                    'rebuild_index',
                    reason='memory maintenance changed indexed content or index is stale',
                )
            )
        return MaintenancePlan(tuple(actions), len(records), budget_after, tuple(rejections))

    def _duplicate_groups(self, records: tuple[MemoryRecord, ...]) -> tuple[tuple[MemoryRecord, ...], ...]:
        groups: dict[tuple[str, str, tuple[str, ...]], list[MemoryRecord]] = {}
        for record in records:
            identity = (
                self._normalise(record.content),
                record.memory_type,
                tuple(sorted(person.casefold() for person in record.people)),
            )
            groups.setdefault(identity, []).append(record)
        return tuple(
            tuple(sorted(group, key=lambda record: record.id))
            for group in groups.values()
            if len(group) > 1
        )

    @staticmethod
    def _normalise(content: str) -> str:
        return re.sub(r'\s+', ' ', content).strip().casefold()

    def _is_expired_temporary(self, record: MemoryRecord, now: datetime) -> bool:
        if record.retention is not RetentionLevel.TEMPORARY or record.evidence:
            return False
        reference_time = record.last_used_at or record.updated_at
        return now - reference_time >= self._budget.temporary_ttl

    def _needs_reindex(
        self,
        actions: list[MaintenanceAction],
        now: datetime,
        index_built_at: datetime | None,
    ) -> bool:
        return bool(actions) or index_built_at is None or now - index_built_at >= self._budget.index_stale_after
