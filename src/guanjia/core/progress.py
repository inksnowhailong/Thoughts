"""从工作程序原始事件生成节流后的任务进度包。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from threading import RLock
from typing import Mapping

from .contracts import WorkerEvent


@dataclass(frozen=True)
class ProgressPolicy:
    """能力目录可声明的进度聚合策略。"""

    kind: str
    min_interval: timedelta
    percent_interval: int

    def __post_init__(self) -> None:
        if self.kind not in {'simple', 'normal', 'long_high_risk'}:
            raise ValueError('unsupported progress policy')
        if not isinstance(self.min_interval, timedelta) or self.min_interval < timedelta(0):
            raise ValueError('min_interval must be a non-negative timedelta')
        if type(self.percent_interval) is not int or not 1 <= self.percent_interval <= 100:
            raise ValueError('percent_interval must be an integer between 1 and 100')

    @classmethod
    def simple(cls) -> 'ProgressPolicy':
        return cls('simple', min_interval=timedelta.max, percent_interval=100)

    @classmethod
    def normal(cls) -> 'ProgressPolicy':
        return cls('normal', min_interval=timedelta(seconds=15), percent_interval=25)

    @classmethod
    def long_high_risk(cls) -> 'ProgressPolicy':
        return cls('long_high_risk', min_interval=timedelta(seconds=30), percent_interval=10)


@dataclass(frozen=True)
class ProgressPackage:
    """给主程序决策的聚合进度，不直接面向用户投递。"""

    task_id: str
    revision: int
    kind: str
    stage: str
    percent: int | None
    source_events: int


class ProgressAggregator:
    """按单任务使用；不会执行任务，也不会决定输出渠道。"""

    def __init__(self, policy: ProgressPolicy) -> None:
        self.policy = policy
        self._identity: tuple[str, int] | None = None
        self._pending_events = 0
        self._last_emitted_percent: int | None = None
        self._last_emitted_stage: str | None = None
        self._last_emitted_at = None
        self._seen_event_ids: dict[str, str] = {}
        self._last_sequence: int | None = None
        self._highest_percent: int | None = None
        self._terminal = False
        self._last_received_at = None
        self._lock = RLock()

    def ingest(self, event: WorkerEvent) -> ProgressPackage | None:
        """接收一个原始事件，必要时返回聚合包。"""
        with self._lock:
            identity = (event.task_id, event.revision)
            if self._identity is None:
                self._identity = identity
            elif self._identity != identity:
                raise ValueError('progress aggregator is bound to one task revision')
            if event.worker_event_id is not None:
                fingerprint = self._fingerprint(event)
                previous = self._seen_event_ids.get(event.worker_event_id)
                if previous is not None:
                    if previous != fingerprint:
                        raise ValueError('worker_event_id cannot be reused with different event content')
                    return None
            if self._terminal:
                raise ValueError('events after terminal worker event are rejected')
            if event.sequence is not None:
                if self._last_sequence is not None and event.sequence <= self._last_sequence:
                    raise ValueError('worker event sequence must strictly increase')
                self._last_sequence = event.sequence
            elif self._last_received_at is not None and event.emitted_at < self._last_received_at:
                raise ValueError('worker event timestamps cannot move backwards')
            self._last_received_at = event.emitted_at
            if event.worker_event_id is not None:
                self._seen_event_ids[event.worker_event_id] = fingerprint
            if event.percent is not None:
                if self._highest_percent is not None and event.percent < self._highest_percent:
                    raise ValueError('progress percent cannot move backwards')
                self._highest_percent = event.percent
            self._pending_events += 1
            if event.event_type in {'completed', 'failed'}:
                self._terminal = True
                return self._package(event, event.event_type, percent=100 if event.event_type == 'completed' else self._highest_percent)
            if event.event_type != 'progress':
                return None
            if self.policy.kind == 'simple':
                return None
            stage_changed = self._last_emitted_stage != event.stage
            percent_changed = (
                event.percent is not None
                and (
                    self._last_emitted_percent is None
                    or event.percent - self._last_emitted_percent >= self.policy.percent_interval
                )
            )
            interval_elapsed = (
                self._last_emitted_at is None
                or event.emitted_at - self._last_emitted_at >= self.policy.min_interval
            )
            if stage_changed or (percent_changed and interval_elapsed):
                return self._package(event, 'progress', percent=self._highest_percent)
            return None

    def _package(self, event: WorkerEvent, kind: str, *, percent: int | None = None) -> ProgressPackage:
        package = ProgressPackage(
            task_id=event.task_id,
            revision=event.revision,
            kind=kind,
            stage=event.stage,
            percent=event.percent if percent is None else percent,
            source_events=self._pending_events,
        )
        self._pending_events = 0
        self._last_emitted_percent = package.percent
        self._last_emitted_stage = event.stage
        self._last_emitted_at = event.emitted_at
        return package

    @staticmethod
    def _fingerprint(event: WorkerEvent) -> str:
        """ID 缺失时按接收顺序并要求 emitted_at 不倒退；有 ID 时严格比较完整语义。"""
        def json_value(value: object) -> object:
            if isinstance(value, Mapping):
                return {key: json_value(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [json_value(item) for item in value]
            return value

        encoded = json.dumps(
            {
                'task_id': event.task_id, 'revision': event.revision,
                'event_type': event.event_type, 'stage': event.stage,
                'emitted_at': event.emitted_at.isoformat(), 'percent': event.percent,
                'payload': json_value(event.payload), 'sequence': event.sequence,
            },
            sort_keys=True, separators=(',', ':'), allow_nan=False,
        )
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
