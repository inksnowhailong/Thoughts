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

    def __init__(self, driver: LocalPtzDriver, position_path: str | Path | None = None) -> None:
        assert_local_ptz_endpoint(driver.endpoint)
        self._driver = driver
        self._lock = RLock()
        self._position_path = Path(position_path or os.environ.get(
            'GUANJIA_POSITION_PATH',
            str(Path(os.environ.get(
                'GUANJIA_VISION_ROOT', r'F:\coding\guanjia-v2-development\data\vision')) / 'position.json'),
        ))

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
        if auto_stop:
            self._estimate_position(horizontal, vertical, duration)
        return cmd

    def stop(self) -> None:
        assert_local_ptz_endpoint(self._driver.endpoint)
        with self._lock:
            self._driver.send_stop()

    @property
    def position(self) -> dict[str, object]:
        """Current software position estimate shared with the vision loop."""
        state = self._load_position()
        return {'pan': float(state.get('pan') or 0.0), 'tilt': float(state.get('tilt') or 0.0),
                'calibrated': bool(state.get('calibrated'))}

    def set_travel(self, travel_sec: float) -> None:
        """Record the measured full-range travel time (speed 0.6) after calibration."""
        with self._lock:
            state = self._load_position()
            state['travel_sec'] = float(travel_sec)
            state['calibrated'] = True
            self._save_position(state)

    def set_position(self, *, pan: float | None = None, tilt: float | None = None) -> None:
        """Re-anchor the absolute position estimate (used right after calibration)."""
        with self._lock:
            state = self._load_position()
            if pan is not None:
                state['pan'] = float(pan)
            if tilt is not None:
                state['tilt'] = float(tilt)
            self._save_position(state)

    def _estimate_position(self, horizontal: float, vertical: float, duration: float) -> None:
        """Accumulate normalized [-1, 1] pan/tilt from a completed move."""
        with self._lock:
            state = self._load_position()
            travel = max(1.0, float(state.get('travel_sec') or 12.0))
            state['pan'] = max(-1.0, min(1.0, float(state.get('pan') or 0.0)
                                + duration * horizontal / (0.6 * travel)))
            state['tilt'] = max(-1.0, min(1.0, float(state.get('tilt') or 0.0)
                                  + duration * vertical / (0.6 * travel)))
            self._save_position(state)

    def _load_position(self) -> dict[str, object]:
        try:
            return json.loads(self._position_path.read_text(encoding='utf-8'))
        except Exception:
            return {'pan': 0.0, 'tilt': 0.0, 'travel_sec': 12.0, 'calibrated': False}

    def _save_position(self, state: dict[str, object]) -> None:
        state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())
        self._position_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._position_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self._position_path)

    def locate(self) -> PtzPose:
        assert_local_ptz_endpoint(self._driver.endpoint)
        with self._lock:
            return self._driver.query_pose()

    def __getattr__(self, name: str) -> object:
        if name.lower() in {'controlptz', 'control_ptz', 'controlmoveptz', 'controllocationptz'}:
            raise AttributeError(f'{name} is forbidden; use local move/stop/locate only')
        raise AttributeError(name)

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import xml.etree.ElementTree as ET


DEFAULT_CAMERA_CONFIG_PATH = Path.home() / '.openclaw' / 'camera-local.json'


@dataclass(frozen=True)
class LocalCameraConfig:
    """Local LAN camera credentials loaded outside the repository."""

    ip: str
    username: str = 'admin'
    password: str = ''
    rtsp_port: int = 554
    http_port: int = 80
    channel: int = 1
    subtype: int = 1  # 1 = sub-stream (low bandwidth for 1-2fps local change detection), 0 = main HD stream
    profile_token: str = 'MediaProfile00000'

    @classmethod
    def from_local_env(cls, config_path: Path | None = None) -> 'LocalCameraConfig':
        path = config_path or Path(os.environ.get('GUANJIA_CAMERA_CONFIG', str(DEFAULT_CAMERA_CONFIG_PATH)))
        data: dict[str, object] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
        ip = str(os.environ.get('GUANJIA_CAMERA_IP') or data.get('ip') or '192.168.10.11').strip()
        username = str(os.environ.get('GUANJIA_CAMERA_USER') or data.get('username') or 'admin').strip()
        password = str(os.environ.get('GUANJIA_CAMERA_PASSWORD') or data.get('password') or '').strip()
        profile_token = str(os.environ.get('GUANJIA_CAMERA_PROFILE') or data.get('profile_token') or 'MediaProfile00000').strip()
        return cls(ip=ip, username=username, password=password, profile_token=profile_token)

    def rtsp_url(self, *, hd: bool = False) -> str:
        stream_sub = 0 if hd else self.subtype
        return f'rtsp://{self.username}:{self.password}@{self.ip}:{self.rtsp_port}/cam/realmonitor?channel={self.channel}&subtype={stream_sub}'

    @property
    def onvif_ptz_url(self) -> str:
        return f'http://{self.ip}:{self.http_port}/onvif/ptz_service'

    @property
    def onvif_media_url(self) -> str:
        return f'http://{self.ip}:{self.http_port}/onvif/media_service'


def build_wsse_header(username: str, password: str, *, nonce_bytes: bytes | None = None, created: str | None = None) -> str:
    """Build standard ONVIF WS-UsernameToken PasswordDigest SOAP header."""
    raw_nonce = nonce_bytes or os.urandom(16)
    timestamp = created or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    digest = hashlib.sha1(raw_nonce + timestamp.encode('ascii') + password.encode('utf-8')).digest()
    nonce_b64 = base64.b64encode(raw_nonce).decode('ascii')
    digest_b64 = base64.b64encode(digest).decode('ascii')
    return (
        '<s:Header>'
        '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        '<UsernameToken>'
        f'<Username>{username}</Username>'
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f'{digest_b64}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f'{nonce_b64}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f'{timestamp}</Created>'
        '</UsernameToken>'
        '</Security>'
        '</s:Header>'
    )


class OnvifPtzDriver:
    """Local LAN ONVIF PTZ driver (never touches Imou cloud API)."""

    def __init__(
        self,
        config: LocalCameraConfig,
        *,
        timeout: float = 3.0,
        sleeper: object = time.sleep,
    ) -> None:
        assert_local_ptz_endpoint(config.onvif_ptz_url)
        self._config = config
        self._timeout = float(timeout)
        self._sleep = sleeper
        self._profile_token: str | None = None

    @property
    def endpoint(self) -> str:
        return self._config.onvif_ptz_url

    def _soap_post(self, url: str, body_xml: str) -> str:
        assert_local_ptz_endpoint(url)
        header = build_wsse_header(self._config.username, self._config.password)
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema" '
            'xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
            f'{header}<s:Body>{body_xml}</s:Body></s:Envelope>'
        )
        req = urllib.request.Request(
            url,
            data=envelope.encode('utf-8'),
            headers={'Content-Type': 'application/soap+xml; charset=utf-8'},
            method='POST',
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=self._timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')

    def resolve_profile_token(self) -> str:
        if self._profile_token:
            return self._profile_token
        try:
            xml_text = self._soap_post(self._config.onvif_media_url, '<trt:GetProfiles/>')
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                if elem.tag.endswith('Profiles') and 'token' in elem.attrib:
                    self._profile_token = elem.attrib['token']
                    return self._profile_token
        except Exception:  # noqa: BLE001
            pass
        self._profile_token = self._config.profile_token
        return self._profile_token

    def send_move(self, command: PtzMoveCommand) -> None:
        token = self.resolve_profile_token()
        body = (
            '<tptz:ContinuousMove>'
            f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
            '<tptz:Velocity>'
            f'<tt:PanTilt x="{command.horizontal:.2f}" y="{command.vertical:.2f}"/>'
            '</tptz:Velocity>'
            '</tptz:ContinuousMove>'
        )
        self._soap_post(self._config.onvif_ptz_url, body)
        if callable(self._sleep) and command.duration > 0:
            self._sleep(command.duration)

    def send_stop(self) -> None:
        token = self.resolve_profile_token()
        body = (
            '<tptz:Stop>'
            f'<tptz:ProfileToken>{token}</tptz:ProfileToken>'
            '<tptz:PanTilt>true</tptz:PanTilt>'
            '<tptz:Zoom>true</tptz:Zoom>'
            '</tptz:Stop>'
        )
        self._soap_post(self._config.onvif_ptz_url, body)

    def query_pose(self) -> PtzPose:
        token = self.resolve_profile_token()
        body = f'<tptz:GetStatus><tptz:ProfileToken>{token}</tptz:ProfileToken></tptz:GetStatus>'
        xml_text = self._soap_post(self._config.onvif_ptz_url, body)
        root = ET.fromstring(xml_text)
        x_val, y_val, z_val = 0.0, 0.0, 0.0
        for elem in root.iter():
            if elem.tag.endswith('PanTilt'):
                x_val = max(-1.0, min(1.0, float(elem.attrib.get('x', '0') or 0.0)))
                y_val = max(-1.0, min(1.0, float(elem.attrib.get('y', '0') or 0.0)))
            elif elem.tag.endswith('Zoom'):
                z_val = max(0.0, min(1.0, float(elem.attrib.get('x', '0') or 0.0)))
        return PtzPose(x_val, y_val, z_val)
