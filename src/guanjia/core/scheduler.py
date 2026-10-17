"""不创建线程或进程的任务调度纯状态机。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .contracts import ResourceRequest, TaskSpec


@dataclass
class _Entry:
    spec: TaskSpec
    capability: str
    state: str = 'queued'


class Scheduler:
    """根据并发和容量租约调度；所有方法只改变内存状态，不启动执行单元。"""

    def __init__(
        self,
        *,
        global_limit: int,
        capability_limits: dict[str, int] | None = None,
        resource_capacities: dict[str, int] | None = None,
    ) -> None:
        if type(global_limit) is not int or global_limit < 1:
            raise ValueError('global_limit must be positive')
        self._global_limit = global_limit
        self._capability_limits = dict(capability_limits or {})
        self._resource_capacities = dict(resource_capacities or {})
        if any(
            not isinstance(capability, str) or not capability
            or type(value) is not int or value < 1
            for capability, value in self._capability_limits.items()
        ):
            raise ValueError('capability limits must use non-empty names and positive integers')
        if any(type(value) is not int or value < 1 for value in self._resource_capacities.values()):
            raise ValueError('resource capacities must be positive integers')
        self._entries: dict[str, _Entry] = {}
        self._preemption_waiters: dict[str, tuple[str, int]] = {}
        self._lock = RLock()

    def submit(self, spec: TaskSpec, *, capability: str) -> None:
        with self._lock:
            if not isinstance(capability, str) or not capability:
                raise ValueError('capability must be a non-empty string')
            if spec.task_id in self._entries:
                raise ValueError(f'task already submitted: {spec.task_id}')
            self._entries[spec.task_id] = _Entry(spec=spec, capability=capability)

    def update_revision(self, spec: TaskSpec) -> None:
        """在未终止前替换调度请求，拒绝回退或跨任务编号。"""
        with self._lock:
            entry = self._entry(spec.task_id)
            if spec.revision <= entry.spec.revision or entry.state not in {'queued', 'paused'}:
                raise ValueError('invalid scheduled task revision update')
            entry.spec = spec
            self._revoke_unneeded_preemptions()

    def dispatch(self) -> tuple[str, ...]:
        """将满足全部约束的 queued 任务移至 running，返回启动顺序。"""
        with self._lock:
            started: list[str] = []
            for entry in self._queued_by_priority():
                if self._can_start(entry):
                    entry.state = 'running'
                    self._revoke_unneeded_preemptions()
                    started.append(entry.spec.task_id)
                else:
                    self._request_preemption_if_useful(entry)
            return tuple(started)

    def _queued_by_priority(self) -> list[_Entry]:
        return sorted(
            (entry for entry in self._entries.values() if entry.state == 'queued'),
            key=lambda entry: (-entry.spec.priority, entry.spec.task_id),
        )

    def _can_start(self, candidate: _Entry) -> bool:
        return self._can_start_with(candidate, self._active())

    def _can_start_with(self, candidate: _Entry, running: tuple[_Entry, ...]) -> bool:
        if len(running) >= self._global_limit:
            return False
        capability_limit = self._capability_limits.get(candidate.capability)
        if capability_limit is not None:
            if sum(item.capability == candidate.capability for item in running) >= capability_limit:
                return False
        held = [
            request
            for active in running
            if active.spec.task_id != candidate.spec.task_id
            for request in active.spec.resources
        ]
        held.extend(
            request
            for entry in self._entries.values()
            if entry.state == 'paused' and entry.spec.task_id != candidate.spec.task_id
            for request in entry.spec.resources
            if not request.release_on_pause
        )
        for request in candidate.spec.resources:
            same = [lease for lease in held if lease.resource == request.resource]
            if request.mode == 'exclusive' and same:
                return False
            if request.mode == 'shared' and any(lease.mode == 'exclusive' for lease in same):
                return False
            capacity = self._resource_capacities.get(request.resource, 1)
            if request.units + sum(lease.units for lease in same) > capacity:
                return False
        return True

    def _request_preemption_if_useful(self, candidate: _Entry) -> None:
        active = self._active()
        candidates = [
            entry for entry in active
            if (
                entry.state == 'running'
                and candidate.spec.priority > entry.spec.priority
                and self._can_pause_for(candidate, entry)
            )
        ]
        selected: list[_Entry] = []
        for entry in sorted(
            candidates,
            key=lambda item: (
                not self._resource_conflict(candidate, item),
                item.spec.priority,
                item.spec.task_id,
            ),
        ):
            selected.append(entry)
            remaining = tuple(item for item in active if item not in selected)
            if self._can_start_with(candidate, remaining):
                for chosen in selected:
                    chosen.state = 'preemption_requested'
                    self._preemption_waiters[chosen.spec.task_id] = (
                        candidate.spec.task_id, candidate.spec.revision,
                    )
                return

    def _resource_conflict(self, candidate: _Entry, active: _Entry) -> bool:
        return any(
            request.resource == held.resource
            for request in candidate.spec.resources
            for held in active.spec.resources
        )

    def _can_pause_for(self, candidate: _Entry, active: _Entry) -> bool:
        """只在高优任务需要释放的冲突资源上要求 pause 释放租约。"""
        for held in active.spec.resources:
            for request in candidate.spec.resources:
                if held.resource != request.resource:
                    continue
                if (
                    held.mode == 'exclusive'
                    or request.mode == 'exclusive'
                    or held.units + request.units > self._resource_capacities.get(held.resource, 1)
                ) and not held.release_on_pause:
                    return False
        return True

    def preemption_requested(self, task_id: str) -> bool:
        with self._lock:
            return self._entry(task_id).state == 'preemption_requested'

    def safe_checkpoint(self, task_id: str) -> bool:
        """工作程序到达安全检查点后协作暂停；调度器绝不强杀步骤。"""
        with self._lock:
            entry = self._entry(task_id)
            if entry.state != 'preemption_requested':
                return False
            waiting = self._preemption_waiters.get(task_id)
            if waiting is None:
                entry.state = 'running'
                return False
            candidate = self._entries.get(waiting[0])
            if candidate is None or candidate.state != 'queued' or candidate.spec.revision != waiting[1]:
                entry.state = 'running'
                self._preemption_waiters.pop(task_id, None)
                return False
            if not self._waiter_still_needs_pause(entry, candidate):
                entry.state = 'running'
                self._preemption_waiters.pop(task_id, None)
                return False
            entry.state = 'paused'
            self._preemption_waiters.pop(task_id, None)
            return True

    def resume(self, task_id: str) -> bool:
        """将暂停任务重新排队；实际恢复仍受普通 dispatch 约束。"""
        with self._lock:
            entry = self._entry(task_id)
            if entry.state != 'paused':
                return False
            entry.state = 'queued'
            return True

    def complete(self, task_id: str) -> None:
        with self._lock:
            entry = self._entry(task_id)
            if entry.state not in {'running', 'preemption_requested'}:
                raise ValueError('only active tasks can complete')
            entry.state = 'completed'

    def cancel(self, task_id: str) -> None:
        with self._lock:
            entry = self._entry(task_id)
            if entry.state in {'completed', 'cancelled'}:
                return
            entry.state = 'cancelled'
            self._revoke_unneeded_preemptions()

    def state_of(self, task_id: str) -> str:
        with self._lock:
            return self._entry(task_id).state

    def _active(self) -> tuple[_Entry, ...]:
        return tuple(
            entry for entry in self._entries.values()
            if entry.state in {'running', 'preemption_requested'}
        )

    def _leased_resources(self, *, exclude: str | None = None) -> tuple[ResourceRequest, ...]:
        leases: list[ResourceRequest] = []
        for entry in self._entries.values():
            if entry.spec.task_id == exclude or entry.state in {'completed', 'cancelled', 'queued'}:
                continue
            for request in entry.spec.resources:
                if entry.state == 'paused' and request.release_on_pause:
                    continue
                leases.append(request)
        return tuple(leases)

    def _entry(self, task_id: str) -> _Entry:
        try:
            return self._entries[task_id]
        except KeyError as exc:
            raise KeyError(f'unknown scheduled task: {task_id}') from exc

    def _revoke_unneeded_preemptions(self) -> None:
        """撤销已经没有排队受益者的协作暂停请求。"""
        for active_task_id, (waiting_task_id, revision) in tuple(self._preemption_waiters.items()):
            waiting = self._entries.get(waiting_task_id)
            active = self._entries.get(active_task_id)
            if (
                waiting is not None
                and waiting.state == 'queued'
                and waiting.spec.revision == revision
                and active is not None
                and active.state == 'preemption_requested'
                and self._waiter_still_needs_pause(active, waiting)
            ):
                continue
            if active is not None and active.state == 'preemption_requested':
                active.state = 'running'
            self._preemption_waiters.pop(active_task_id, None)

    def _waiter_still_needs_pause(self, active: _Entry, waiting: _Entry) -> bool:
        """绑定 waiter 仍排队、优先级更高，且释放该任务后才可能启动。"""
        if waiting.state != 'queued' or waiting.spec.priority <= active.spec.priority:
            return False
        running = tuple(item for item in self._active() if item.spec.task_id != active.spec.task_id)
        if not self._can_start_with(waiting, running):
            return False
        return self._can_pause_for(waiting, active)
