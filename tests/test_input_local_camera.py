from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.input.adapters.local_camera import LocalOnvifCameraSampler
from guanjia.output.adapters.camera_ptz import LocalCameraConfig


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_offline_and_bad_credentials_degrade_without_raising() -> None:
    offline_cfg = LocalCameraConfig(ip='127.0.0.1', http_port=9, rtsp_port=9, password='invalid')
    sampler = LocalOnvifCameraSampler(offline_cfg, timeout=0.2)
    check(sampler.check_rtsp_reachable() is False, 'unreachable RTSP returns False without raising')
    outcome = sampler.sample_once()
    check(outcome.online is False and outcome.degraded_reason is not None, 'offline camera degrades cleanly')
    check(outcome.visual_request is None, 'degraded camera never triggers vision model')


if __name__ == '__main__':
    test_offline_and_bad_credentials_degrade_without_raising()
    print('ALL PASS test_input_local_camera')
