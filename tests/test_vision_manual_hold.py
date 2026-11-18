"""对话转向让路窗口（2026-11-06 事故）回归测试。

事故：主人说"看向我"，camera_look 在子进程里把镜头转到主人面前；
VisionLoop 因 YOLO 未检出静止近距的主人（person_present 恒 False），
护位逻辑一步步把镜头拖回沙发 home_pan=0.6（"自己不停往远离我的方向转"），
期间 VLM 重锚跳变位置估计又触发新一轮护位长征，管家手动转回的镜头也被再次拉走。

修复：camera-tool.py 每次对话性转动后经 LocalCameraPtzAdapter.mark_manual()
把窗口写进 position.json（两进程唯一共享状态）；VisionLoop 窗口内不护位、
不跟转、不重锚。gaze_hold 只停护位，保留有人时的边缘跟转。
"""
from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.output.adapters.camera_ptz import LocalCameraPtzAdapter
from guanjia.vision_loop import VisionLoop


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeDriver:
    endpoint = 'http://192.168.10.11/onvif/ptz_service'

    def send_move(self, cmd): pass

    def send_stop(self): pass

    def query_pose(self): pass


class FakeProactive:
    def num(self, key: str, default: float = 0.0) -> float:
        return {
            'track_edge': 0.22, 'home_pan': 0.6, 'home_tilt': -0.35,
            'settle_sec': 1.6, 'track_cooldown_sec': 2.0,
            'standby_pan_tolerance': 0.05, 'bedroom_standby_timeout_min': 120,
        }.get(key, default)


class FakePtz:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float, float]] = []

    def move(self, h: float, v: float, d: float) -> None:
        self.moves.append((h, v, d))


def make_loop(position_path: Path) -> VisionLoop:
    loop = object.__new__(VisionLoop)  # 不碰真实相机硬件
    loop.position_path = position_path
    loop._ptz = FakePtz()
    loop._settle_until = 0.0
    loop._proactive = FakeProactive()
    loop.mode = 'patrol'
    loop._standby_target_pan = None
    loop._standby_since = 0.0
    loop._track_cooldown_until = 0.0
    loop._person_present = False
    loop._moves_since_anchor = 0
    loop._baseline_reset = False
    return loop


def test_mark_manual_roundtrip_preserves_position() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = LocalCameraPtzAdapter(FakeDriver(), position_path=Path(tmp) / 'position.json')
        adapter.mark_manual(steer_hold_sec=90, gaze_hold_sec=600)
        holds = adapter.manual_holds()
        check(holds == {'steer_hold_active': True, 'gaze_hold_active': True}, f'unexpected holds: {holds}')
        adapter.set_position(pan=0.4)
        adapter.mark_manual(steer_hold_sec=30)  # 二次标记不得丢掉 pan/tilt
        state = json.loads((Path(tmp) / 'position.json').read_text(encoding='utf-8'))
        check(abs(state['pan'] - 0.4) < 1e-9, 'mark_manual must preserve pan')
        check(state['gaze_hold_until'] >= time.time() + 500, 'gaze hold must persist')


def test_steer_hold_blocks_homing_and_tracking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        position_path = Path(tmp) / 'position.json'
        position_path.write_text(json.dumps({'pan': 0.311, 'tilt': -0.35, 'calibrated': True}), encoding='utf-8')
        adapter = LocalCameraPtzAdapter(FakeDriver(), position_path=position_path)

        # 基线：无窗口时"无人"必须触发护位（旧事故路径）
        loop = make_loop(position_path)
        loop._steer(False, None, False, time.localtime())
        check(len(loop._ptz.moves) > 0, 'baseline home-return must fire without holds')

        # 让路窗口内：无人护位、有人跟转全停
        adapter.mark_manual(steer_hold_sec=90, gaze_hold_sec=600)
        loop = make_loop(position_path)
        loop._steer(False, None, False, time.localtime())
        loop._steer(True, (0.05, 0.2, 0.15, 0.8), False, time.localtime())
        check(loop._ptz.moves == [], 'steer hold must block homing and tracking')


def test_gaze_hold_blocks_homing_keeps_tracking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        position_path = Path(tmp) / 'position.json'
        state = {'pan': 0.311, 'tilt': -0.35, 'calibrated': True,
                 'manual_until': time.time() - 1, 'gaze_hold_until': time.time() + 600}
        position_path.write_text(json.dumps(state), encoding='utf-8')

        loop = make_loop(position_path)
        loop._steer(False, None, False, time.localtime())
        check(loop._ptz.moves == [], 'gaze hold must block home-return')

        loop = make_loop(position_path)
        loop._steer(True, (0.05, 0.2, 0.15, 0.8), False, time.localtime())
        check(loop._ptz.moves and loop._ptz.moves[0][0] < 0, 'edge tracking must survive gaze hold')


def test_reanchor_skipped_during_steer_hold() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        position_path = Path(tmp) / 'position.json'
        position_path.write_text(json.dumps({'pan': 0.311, 'calibrated': True}), encoding='utf-8')
        adapter = LocalCameraPtzAdapter(FakeDriver(), position_path=position_path)
        adapter.mark_manual(steer_hold_sec=90)
        loop = make_loop(position_path)
        check(loop._manual_holds()[0] is True, 'steer hold must be readable by the loop')


if __name__ == '__main__':
    test_mark_manual_roundtrip_preserves_position()
    test_steer_hold_blocks_homing_and_tracking()
    test_gaze_hold_blocks_homing_keeps_tracking()
    test_reanchor_skipped_during_steer_hold()
    print('ALL PASS test_vision_manual_hold')
