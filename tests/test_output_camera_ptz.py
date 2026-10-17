from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.core.contracts import TaskSpec
from guanjia.core.scheduler import Scheduler
from guanjia.output.adapters.camera_ptz import (
    CAMERA_PTZ_RESOURCE,
    LocalCameraPtzAdapter,
    PtzMoveCommand,
    PtzPose,
    camera_ptz_resource_request,
)


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeLocalDriver:
    def __init__(self, endpoint: str = 'onvif://192.168.1.108:80/onvif/ptz_service') -> None:
        self.endpoint = endpoint
        self.events: list[str] = []
        self.pose = PtzPose(0.0, 0.0, 0.0)

    def send_move(self, command: PtzMoveCommand) -> None:
        self.events.append(f'move:{command.horizontal}:{command.vertical}:{command.duration}')
        self.pose = PtzPose(command.horizontal, command.vertical, 0.0)

    def send_stop(self) -> None:
        self.events.append('stop')

    def query_pose(self) -> PtzPose:
        self.events.append('locate')
        return self.pose


def make_ptz_task(task_id: str, priority: int = 1) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        revision=1,
        goal=f'ptz-{task_id}',
        priority=priority,
        resources=(camera_ptz_resource_request(),),
    )


def test_local_ptz_moves_stops_and_rejects_cloud_control_ptz() -> None:
    driver = FakeLocalDriver()
    adapter = LocalCameraPtzAdapter(driver)
    cmd = adapter.move(0.5, -0.25, 0.4)
    check(cmd == PtzMoveCommand(0.5, -0.25, 0.4), 'move returns validated command')
    check(driver.events == ['move:0.5:-0.25:0.4', 'stop'], 'short move immediately auto-stops')
    pose = adapter.locate()
    check(pose == PtzPose(0.5, -0.25, 0.0), 'locate returns current pose')

    for bad_endpoint in (
        'https://openapi.lechange.cn/openapi/controlMovePTZ',
        'https://openapi.imou.com/openapi/controlPTZ',
    ):
        try:
            LocalCameraPtzAdapter(FakeLocalDriver(endpoint=bad_endpoint))
        except ValueError:
            pass
        else:
            raise AssertionError(f'cloud PTZ endpoint was not rejected: {bad_endpoint}')

    try:
        getattr(adapter, 'controlPTZ')
    except AttributeError:
        pass
    else:
        raise AssertionError('controlPTZ attribute must be forbidden')


def test_scheduler_enforces_exclusive_camera_ptz_resource() -> None:
    scheduler = Scheduler(global_limit=4, resource_capacities={CAMERA_PTZ_RESOURCE: 1})
    scheduler.submit(make_ptz_task('worker-ptz-1', priority=5), capability='camera.ptz')
    scheduler.submit(make_ptz_task('worker-ptz-2', priority=4), capability='camera.ptz')

    started = scheduler.dispatch()
    check(started == ('worker-ptz-1',), 'only one worker can hold exclusive camera.ptz at a time')


if __name__ == '__main__':
    test_local_ptz_moves_stops_and_rejects_cloud_control_ptz()
    test_scheduler_enforces_exclusive_camera_ptz_resource()
    print('ALL PASS test_output_camera_ptz')
