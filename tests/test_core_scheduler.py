"""Pure scheduler state-machine contract tests."""

from __future__ import annotations

from guanjia.core.contracts import ResourceRequest, TaskSpec
from guanjia.core.scheduler import Scheduler


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


scheduler = Scheduler(global_limit=1, capability_limits={'agent': 1})
low = TaskSpec(
    task_id='low',
    revision=1,
    goal='background work',
    priority=1,
    resources=(ResourceRequest('desktop', mode='exclusive', release_on_pause=True),),
)
high = TaskSpec(
    task_id='high',
    revision=1,
    goal='urgent work',
    priority=10,
    resources=(ResourceRequest('desktop', mode='exclusive'),),
)
second_agent = TaskSpec(task_id='second', revision=1, goal='another', priority=2)

scheduler.submit(low, capability='agent')
check(scheduler.dispatch() == ('low',), 'dispatches ready task')
scheduler.submit(second_agent, capability='agent')
check(scheduler.dispatch() == (), 'enforces capability concurrency')
scheduler.submit(high, capability='system')
check(scheduler.dispatch() == (), 'does not forcefully preempt a running task')
check(scheduler.preemption_requested('low'), 'requests cooperative preemption for priority conflict')
check(scheduler.safe_checkpoint('low'), 'checkpoint releases preemptible task resources')
check(scheduler.dispatch() == ('high',), 'runs high priority task after safe checkpoint')
scheduler.complete('high')
check(scheduler.dispatch() == ('second',), 'releases global and capability slots on completion')
check(scheduler.resume('low'), 'paused task can re-enter the queue')

capacity = Scheduler(global_limit=3, resource_capacities={'gpu': 2})
capacity.submit(
    TaskSpec(
        task_id='gpu-a', revision=1, goal='one',
        resources=(ResourceRequest('gpu', units=2),),
    ),
    capability='worker',
)
capacity.submit(
    TaskSpec(
        task_id='gpu-b', revision=1, goal='two',
        resources=(ResourceRequest('gpu', units=1),),
    ),
    capability='worker',
)
check(capacity.dispatch() == ('gpu-a',), 'resource capacity accounts for shared units')
capacity.update_revision(TaskSpec(task_id='gpu-b', revision=2, goal='revised two'))
check(capacity.state_of('gpu-b') == 'queued', 'scheduler accepts forward task revisions')
capacity.dispatch()
try:
    capacity.update_revision(TaskSpec(task_id='gpu-a', revision=2, goal='must not replace active work'))
except ValueError:
    pass
else:
    raise AssertionError('scheduler only updates queued or paused revisions')

global_preemption = Scheduler(global_limit=3)
for task_id, priority in (('low-a', 1), ('low-b', 2), ('low-c', 3)):
    global_preemption.submit(
        TaskSpec(task_id=task_id, revision=1, goal=task_id, priority=priority),
        capability='worker',
    )
check(global_preemption.dispatch() == ('low-c', 'low-b', 'low-a'), 'fills global slots')
global_preemption.submit(
    TaskSpec(task_id='urgent', revision=1, goal='urgent', priority=10),
    capability='worker',
)
check(global_preemption.dispatch() == (), 'full global limit delays urgent task cooperatively')
check(
    global_preemption.preemption_requested('low-a')
    and not global_preemption.preemption_requested('low-b')
    and not global_preemption.preemption_requested('low-c'),
    'global preemption requests only the minimum lowest-priority task',
)
global_preemption.submit(
    TaskSpec(task_id='lower-waiter', revision=1, goal='lower', priority=5),
    capability='worker',
)
global_preemption.dispatch()
check(
    global_preemption.preemption_requested('low-a'),
    'a lower-priority waiter cannot replace an existing preemption binding',
)
check(global_preemption.safe_checkpoint('low-a'), 'minimum selected task can pause')
check(global_preemption.dispatch() == ('urgent',), 'urgent task starts after one global slot releases')

try:
    Scheduler(global_limit=1).submit(
        TaskSpec(
            task_id='duplicate-resource', revision=1, goal='invalid',
            resources=(ResourceRequest('gpu'), ResourceRequest('gpu')),
        ),
        capability='worker',
    )
except ValueError:
    pass
else:
    raise AssertionError('same task cannot request the same resource twice')

print('ALL PASS test_core_scheduler')
