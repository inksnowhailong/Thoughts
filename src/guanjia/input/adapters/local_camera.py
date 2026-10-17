"""Local LAN camera input sampler (ONVIF snapshot + RTSP check + 1-2fps change gate).

Guarantees required by Task 8 / Task 10:
- Uses local ONVIF GetSnapshotUri + HTTP Digest/Basic auth and RTSP handshake.
- Decodes frames locally at 1-2 fps and computes normalized pixel change score.
- Feeds VisualCandidateGate so static scenes never invoke the vision model.
- Network errors, device offline, or expired credentials degrade gracefully
  without raising into or blocking the main program.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import socket
import urllib.request
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image

from guanjia.input.contracts import MediaRef
from guanjia.output.adapters.camera_ptz import LocalCameraConfig, OnvifPtzDriver
from guanjia.self_loop.visual import LocalVisualSignal, VisualCandidateGate, VisualDescriptionRequest


@dataclass(frozen=True)
class CameraSampleOutcome:
    """Result of one non-blocking local camera poll."""

    online: bool
    degraded_reason: str | None
    media_ref: MediaRef | None
    change_score: float
    visual_request: VisualDescriptionRequest | None


class LocalOnvifCameraSampler:
    """1-2 fps local snapshot sampler with graceful offline/auth degradation."""

    def __init__(
        self,
        config: LocalCameraConfig | None = None,
        *,
        scene_id: str = 'home-camera',
        gate: VisualCandidateGate | None = None,
        timeout: float = 3.0,
    ) -> None:
        self._config = config or LocalCameraConfig.from_local_env()
        self._scene_id = scene_id
        self._gate = gate or VisualCandidateGate()
        self._timeout = float(timeout)
        self._driver = OnvifPtzDriver(self._config, timeout=self._timeout)
        self._snapshot_uri: str | None = None
        self._prev_gray: np.ndarray | None = None

    def check_rtsp_reachable(self) -> bool:
        """Verify local RTSP port 554 responds without downloading video stream."""
        try:
            with socket.create_connection((self._config.ip, self._config.rtsp_port), timeout=self._timeout) as sock:
                crlf = chr(13) + chr(10)
                req = (
                    f'OPTIONS rtsp://{self._config.ip}:{self._config.rtsp_port}/cam/realmonitor?channel=1&subtype=1 RTSP/1.0'
                    + crlf
                    + 'CSeq: 1'
                    + crlf
                    + crlf
                )
                sock.sendall(req.encode('ascii'))
                line = sock.recv(256).decode('ascii', errors='replace')
                return line.startswith('RTSP/1.0')
        except Exception:  # noqa: BLE001
            return False

    def resolve_snapshot_uri(self) -> str:
        if self._snapshot_uri:
            return self._snapshot_uri
        token = self._driver.resolve_profile_token()
        xml_text = self._driver._soap_post(
            self._config.onvif_media_url,
            f'<trt:GetSnapshotUri><trt:ProfileToken>{token}</trt:ProfileToken></trt:GetSnapshotUri>',
        )
        root = ET.fromstring(xml_text)
        for elem in root.iter():
            if elem.tag.endswith('Uri') and elem.text:
                self._snapshot_uri = elem.text.strip()
                return self._snapshot_uri
        self._snapshot_uri = f'http://{self._config.ip}:{self._config.http_port}/onvifsnapshot/media_service/snapshot'
        return self._snapshot_uri

    def sample_once(self, *, now: datetime | None = None) -> CameraSampleOutcome:
        """Fetch one frame, compute local change score, and degrade safely on any failure."""
        observed_at = now or datetime.now(timezone.utc)
        try:
            uri = self.resolve_snapshot_uri()
            pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            pwd_mgr.add_password(None, f'http://{self._config.ip}/', self._config.username, self._config.password)
            pwd_mgr.add_password(None, uri, self._config.username, self._config.password)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPDigestAuthHandler(pwd_mgr),
                urllib.request.HTTPBasicAuthHandler(pwd_mgr),
            )
            with opener.open(uri, timeout=self._timeout) as resp:
                frame_bytes = resp.read()
            if not frame_bytes:
                return CameraSampleOutcome(False, 'empty_snapshot', None, 0.0, None)
            img = Image.open(BytesIO(frame_bytes)).convert('L').resize((64, 36))
            gray = np.asarray(img, dtype=np.float32) / 255.0
            if self._prev_gray is None:
                change_score = 0.0
            else:
                change_score = float(np.clip(np.mean(np.abs(gray - self._prev_gray)) * 4.0, 0.0, 1.0))
            self._prev_gray = gray
            sha = hashlib.sha256(frame_bytes).hexdigest()
            ref = MediaRef(sha, sha, 'image', len(frame_bytes))
            signal = LocalVisualSignal(
                scene_id=self._scene_id,
                media_ref=ref,
                observed_at=observed_at,
                change_score=change_score,
            )
            visual_req = self._gate.request(signal)
            return CameraSampleOutcome(True, None, ref, change_score, visual_req)
        except Exception as exc:  # noqa: BLE001
            return CameraSampleOutcome(
                online=False,
                degraded_reason=f'{type(exc).__name__}',
                media_ref=None,
                change_score=0.0,
                visual_request=None,
            )
