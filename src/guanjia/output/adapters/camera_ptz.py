"""Local camera PTZ output port and exclusive resource scheduling guard.

Per Global Constraints and Wave 3/5 Task 9:
- Provides move(horizontal, vertical, duration), stop(), and locate().
- Strictly forbids Imou cloud controlPTZ / controlMovePTZ / controlLocationPTZ.
- Integrates with Scheduler using an exclusive `camera.ptz` ResourceRequest so
  two workers can never control the camera gimbal simultaneously.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import RLock
from typing import Mapping, Protocol

from guanjia.core.contracts import ResourceRequest


CAMERA_PTZ_RESOURCE = 'camera.ptz'
FORBIDDEN_CLOUD_PTZ_ENDPOINTS = frozenset({
    'controlptz',
    'controlmoveptz',
    'controllocationptz',
    'openapi.lechange.cn',
    'openapi.imou.com',
})


def camera_ptz_resource_request(*, release_on_pause: bool = True) -> ResourceRequest:
    """Build the canonical exclusive Scheduler resource request for PTZ control."""
    return ResourceRequest(
        resource=CAMERA_PTZ_RESOURCE,
        mode='exclusive',
        units=1,
        release_on_pause=release_on_pause,
    )


def assert_local_ptz_endpoint(endpoint_or_command: str) -> None:
    """Reject any attempt to route PTZ commands through Imou cloud endpoints."""
    if not isinstance(endpoint_or_command, str) or not endpoint_or_command.strip():
        raise ValueError('endpoint_or_command must be a non-empty string')
    lowered = endpoint_or_command.strip().lower()
    for forbidden in FORBIDDEN_CLOUD_PTZ_ENDPOINTS:
        if forbidden in lowered:
            raise ValueError(f'cloud PTZ is forbidden by architecture policy: {forbidden}')


@dataclass(frozen=True)
class PtzMoveCommand:
    """Validated local gimbal movement command."""

    horizontal: float
    vertical: float
    duration: float

    def __post_init__(self) -> None:
        for name, value in (('horizontal', self.horizontal), ('vertical', self.vertical)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{name} must be a finite number')
            if not -1.0 <= float(value) <= 1.0:
                raise ValueError(f'{name} must be within [-1.0, 1.0]')
        if isinstance(self.duration, bool) or not isinstance(self.duration, (int, float)) or not math.isfinite(self.duration):
            raise ValueError('duration must be a finite number')
        if not 0.0 < float(self.duration) <= 5.0:
            raise ValueError('duration must be within (0.0, 5.0] seconds')
        object.__setattr__(self, 'horizontal', float(self.horizontal))
        object.__setattr__(self, 'vertical', float(self.vertical))
        object.__setattr__(self, 'duration', float(self.duration))


@dataclass(frozen=True)
class PtzPose:
    """Normalized local gimbal pose."""

    horizontal: float
    vertical: float
    zoom: float = 0.0

    def __post_init__(self) -> None:
        for name, value, low, high in (
            ('horizontal', self.horizontal, -1.0, 1.0),
            ('vertical', self.vertical, -1.0, 1.0),
            ('zoom', self.zoom, 0.0, 1.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{name} must be a finite number')
            if not low <= float(value) <= high:
                raise ValueError(f'{name} must be within [{low}, {high}]')
            object.__setattr__(self, name, float(value))


class LocalPtzDriver(Protocol):
    """Low-level local protocol driver (e.g., ONVIF / local CGI), never cloud PTZ."""

    @property
    def endpoint(self) -> str: ...

    def send_move(self, command: PtzMoveCommand) -> None: ...

    def send_stop(self) -> None: ...

    def query_pose(self) -> PtzPose: ...


class CameraPtzPort(Protocol):
    """Output port for camera PTZ control."""

    def move(self, horizontal: float, vertical: float, duration: float, *, auto_stop: bool = True) -> PtzMoveCommand: ...

    def stop(self) -> None: ...

    def locate(self) -> PtzPose: ...


class LocalCameraPtzAdapter:
    """Thread-safe local PTZ adapter that enforces auto-stop and forbids cloud PTZ."""

    def __init__(self, driver: LocalPtzDriver) -> None:
        assert_local_ptz_endpoint(driver.endpoint)
        self._driver = driver
        self._lock = RLock()

    def move(
        self,
        horizontal: float,
        vertical: float,
        duration: float,
        *,
        auto_stop: bool = True,
    ) -> PtzMoveCommand:
        assert_local_ptz_endpoint(self._driver.endpoint)
        cmd = PtzMoveCommand(horizontal=horizontal, vertical=vertical, duration=duration)
        with self._lock:
            try:
                self._driver.send_move(cmd)
            finally:
                if auto_stop:
                    self._driver.send_stop()
        return cmd

    def stop(self) -> None:
        assert_local_ptz_endpoint(self._driver.endpoint)
        with self._lock:
            self._driver.send_stop()

    def locate(self) -> PtzPose:
        assert_local_ptz_endpoint(self._driver.endpoint)
        with self._lock:
            return self._driver.query_pose()

    def __getattr__(self, name: str) -> object:
        if name.lower() in {'controlptz', 'control_ptz', 'controlmoveptz', 'controllocationptz'}:
            raise AttributeError(f'{name} is forbidden; use local move/stop/locate only')
        raise AttributeError(name)
