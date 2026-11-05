"""Passive camera loop: adaptive capture, change score, person detection, position-aware context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import urllib.request

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageChops

from guanjia.output.adapters.camera_ptz import LocalCameraConfig, LocalCameraPtzAdapter, OnvifPtzDriver
from guanjia.proactive.config import ProactiveConfig, is_night_hour
from guanjia.proactive.privacy import PrivacyDownscaler


@dataclass(frozen=True)
class VisionLoopConfig:
    root: Path = Path(os.environ.get(
        'GUANJIA_VISION_ROOT',
        r'F:\coding\guanjia-v2-development\data\vision',
    ))
    model_path: Path = Path(os.environ.get(
        'GUANJIA_PERSON_MODEL',
        r'F:\coding\guanjia-v2-development\data\models\yolov8n.onnx',
    ))
    normal_interval_sec: float = 5.0
    changed_interval_sec: float = 3.0
    person_interval_sec: float = 1.0
    max_hours: float = 3.0
    max_frames: int = 5000
    max_bytes: int = 2 * 1024 * 1024 * 1024
    change_threshold: float = 0.055
    person_threshold: float = 0.40


class PersonDetector:
    """YOLO person-only inference; no agent or remote request."""

    def __init__(self, model_path: Path, confidence: float) -> None:
        if not model_path.exists():
            raise FileNotFoundError(f'person detector model missing: {model_path}')
        self._session = ort.InferenceSession(str(model_path), providers=['CPUExecutionProvider'])
        self._input = self._session.get_inputs()[0]
        shape = self._input.shape
        self._size = int(shape[-1]) if isinstance(shape[-1], int) else 640
        self._confidence = confidence

    def detect(self, image: Image.Image) -> tuple[bool, float, tuple[float, float, float, float] | None]:
        """返回 (是否有人, 最高分, 归一化人框 (x1,y1,x2,y2) 或 None)；框供隐私降级与跟转用。"""
        source = image.convert('RGB')
        scale = min(self._size / source.width, self._size / source.height)
        width = max(1, round(source.width * scale))
        height = max(1, round(source.height * scale))
        resized = source.resize((width, height), Image.Resampling.BILINEAR)
        canvas = Image.new('RGB', (self._size, self._size), (114, 114, 114))
        canvas.paste(resized, ((self._size - width) // 2, (self._size - height) // 2))
        tensor = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        output = np.squeeze(self._session.run(None, {self._input.name: tensor})[0])
        if output.ndim != 2:
            return False, 0.0, None
        if output.shape[0] < output.shape[1] and output.shape[0] <= 100:
            output = output.T
        if output.shape[1] == 84:  # YOLOv8: box + 80 class scores; column 4 is the person class
            scores = output[:, 4]
        elif output.shape[1] >= 85:  # YOLOv5-style: box + objectness + classes
            scores = output[:, 4] * output[:, 5]
        else:
            return False, 0.0, None
        if not scores.size:
            return False, 0.0, None
        best = int(np.argmax(scores))
        score = float(scores[best])
        if score < self._confidence:
            return False, score, None
        raw = output[best, :4]
        # YOLOv8 ONNX 导出为 cx,cy,w,h：先转角坐标 xyxy
        cx, cy, bw, bh = float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])
        box_x1, box_y1 = cx - bw / 2.0, cy - bh / 2.0
        box_x2, box_y2 = cx + bw / 2.0, cy + bh / 2.0
        # 画布坐标 → 原图坐标（去 letterbox padding）
        pad_x = (self._size - width) / 2.0
        pad_y = (self._size - height) / 2.0
        x1 = (box_x1 - pad_x) / scale
        y1 = (box_y1 - pad_y) / scale
        x2 = (box_x2 - pad_x) / scale
        y2 = (box_y2 - pad_y) / scale
        norm = (
            max(0.0, min(1.0, x1 / source.width)),
            max(0.0, min(1.0, y1 / source.height)),
            max(0.0, min(1.0, x2 / source.width)),
            max(0.0, min(1.0, y2 / source.height)),
        )
        return True, score, norm


def area_for_pan(pan: float | None, zones: list[dict[str, object]]) -> str:
    """Map a normalized pan value to a room-area label using room-map.json zones."""
    if pan is None or not zones:
        return ''
    for zone in zones:
        try:
            if float(zone['pan_min']) <= pan < float(zone['pan_max']):
                return str(zone['area'])
        except (KeyError, TypeError, ValueError):
            continue
    for zone in reversed(zones):  # clamp: pan at/beyond the outermost edge maps to the last zone
        try:
            if pan >= float(zone['pan_min']):
                return str(zone['area'])
        except (KeyError, TypeError, ValueError):
            continue
    return ''


def vision_note(record: dict[str, object], now: datetime | None = None) -> str:
    """Compact one-line visual context appended to a voice turn, e.g. [视觉|朝向沙发/客厅区|画面变化很小,未检测到人|3秒前]."""
    if not isinstance(record, dict) or not record.get('ok'):
        return ''
    parts = []
    area = str(record.get('area') or '').strip()
    if area:
        parts.append(f'朝向{area}')
    summary = str(record.get('summary') or '').strip()
    if summary:
        parts.append(summary)
    captured = record.get('ts')
    if captured:
        try:
            delta = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(str(captured))
            seconds = max(0, int(delta.total_seconds()))
            if seconds < 90:
                parts.append(f'{seconds}秒前')
            elif seconds < 5400:
                parts.append(f'{seconds // 60}分钟前')
            else:
                parts.append(f'{seconds // 3600}小时前')
        except (ValueError, TypeError):
            pass
    return '[视觉|' + '|'.join(parts) + ']' if parts else ''


class VisionLoop:
    """Maintains the latest visual state and a bounded local frame buffer."""

    def __init__(self, config: VisionLoopConfig | None = None) -> None:
        self.config = config or VisionLoopConfig()
        self.frames_dir = self.config.root / 'frames'
        self.latest_path = self.config.root / 'latest.json'
        self.health_path = self.config.root / 'health.json'
        self.db_path = self.config.root / 'vision.sqlite'
        self.position_path = self.config.root / 'position.json'
        self.room_map_path = self.config.root / 'room-map.json'
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self._camera = LocalCameraConfig.from_local_env()
        self._detector = PersonDetector(self.config.model_path, self.config.person_threshold)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_small: Image.Image | None = None
        self._person_present = False
        self._positive_hits = 0
        self._negative_hits = 0
        self._room_zones: list[dict[str, object]] | None = None
        # ---- 主动环地基（议会定稿：隐私降级组合路 + 夜间静默 + 卧室待命 + 停稳再拍）----
        self._proactive = ProactiveConfig()
        self._downscaler = PrivacyDownscaler(
            self._proactive.light_scale, self._proactive.deep_scale)
        self._ptz: LocalCameraPtzAdapter | None = self._init_ptz()
        self.mode = 'patrol'                      # patrol | standby（夜间叠加判定，见 _is_night）
        self._standby_target_pan: float | None = None
        self._standby_since: float = 0.0          # monotonic，待命超时回巡逻
        self._settle_until: float = 0.0           # PTZ 转动中不抓帧（停稳再拍）
        self._baseline_reset = False               # 转动后首帧 change_score 置零（自转排除）
        self._leave_deep_until: float = 0.0        # 离开沿后 N 分钟深度降级窗
        self._track_cooldown_until: float = 0.0   # 跟转切换迟滞（防摆动）
        # ---- 误检治理（2026-11-04 事故：沙发绿玩偶被 YOLO 0.81 置信连认 6 小时"人"，
        # 镜头锁死沙发位，真人在别处说话也不理）：动静门控 + 静止疑似 VLM 仲裁 ----
        self._static_since: float = 0.0            # 画面持续静止的起点（monotonic，0=有动静）
        self._static_lock = False                  # 已被仲裁判为静物：锁定至画面有动静
        self._person_check_at: float = 0.0         # VLM 仲裁限频（60 秒一次）
        # ---- 周期重锚（省额度版，2026-11-04 发起人问模型消耗后收紧）：
        # 位置漂移只来自转动指令的积分误差——"累计转过 ≥N 步"才值得重锚，
        # 12 小时兜底一次即可；没转镜头的日子 0 次调用 ----
        self._reanchor_at: float = time.monotonic() + max(
            3600.0, self._proactive.num('reanchor_interval_sec', 43200))
        self._moves_since_anchor = 0                # 上次重锚后累计的转动步数
        self._person_checks_today = 0               # 当日真人仲裁调用数（日预算闸）
        self._person_checks_date = time.strftime('%Y-%m-%d')
        self.on_edge: object | None = None        # 沿回调钩子：fn(record, edge) edge∈{entered,left}——A 身份感知接线用
        self.on_tick: object | None = None        # 每帧轻回调：fn(record)——在场身份 TTL 续期（V3）用；回调必须无阻塞
        # 独处计时（发起人定 1 小时）：人持续在场且无沿 → 模拟一次 entered 沿
        self._alone_since: float = 0.0            # 本轮持续在场的起点（monotonic，0=不在场）
        self._alone_edge_fired = False             # 本轮独处计时是否已发过沿（发一次就够，不重复）
        self._init_db()

    def _init_ptz(self) -> LocalCameraPtzAdapter | None:
        """PTZ 惰性初始化；相机不在/ONVIF 不通时视觉循环照常拍照，只是不转。"""
        try:
            driver = OnvifPtzDriver(self._camera)
            adapter = LocalCameraPtzAdapter(driver)
        except Exception:
            return None
        # 启动重锚（2026-11-03 发起人反馈"总是默认面壁"的另一半根因）：
        # 摄像头断电重启后云台回硬件默认位（正对显示器/墙），position.json 却还记着
        # 旧 pan；且本机 ONVIF GetStatus 恒为 (0,0)，没有绝对位置反馈，脱节后无人护位
        # 永远纠不正。改调 camera-tool.py locate 的 VLM 实拍自定位（认显示器/冰箱/
        # 沙发等参照物 → 绝对 pan）重锚软件估计；失败沿用旧估计，绝不拖垮视觉循环。
        try:
            anchor = self._vlm_reanchor()
            if anchor is not None:
                adapter.set_position(pan=anchor)
        except Exception:
            pass
        return adapter

    def _vlm_reanchor(self) -> float | None:
        """调 camera-tool.py locate 让 VLM 认参照物，返回重锚后的绝对 pan（失败 None）。"""
        import subprocess
        tool = Path(r'F:\coding\guanjia-v2-development\openclaw\workspace\tools\camera-tool.py')
        try:
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', str(tool), 'locate'],
                capture_output=True, text=True, timeout=45,
            )
        except Exception:
            return None
        for line in reversed((result.stdout or '').splitlines()):
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data.get('pan'), (int, float)):
                return float(data['pan'])
            return None
        return None

    def _person_gate(self, detected: bool, changed: bool, change_score: float) -> bool:
        """YOLO 命中 → 有效在场判定：动静门控 + 静止疑似 VLM 仲裁。

        设计（针对 2026-11-04 玩偶误检事故）：
        - 有动静的检测直接采信（人走动/进入必然带动画面变化）；
        - 静止场景的检测不可信（玩偶/抱枕/海报常年 0.8 置信）：新入场无动静、
          或在场但画面静止超时 → 交 VLM 仲裁"是不是真人"；
        - 判为静物后锁定，直到画面重新有动静才解锁（防每帧重复仲裁）。
        """
        if not detected:
            self._static_since = 0.0
            return False
        if changed or change_score >= self.config.change_threshold:
            self._static_since = 0.0
            self._static_lock = False
            return True
        if self._static_lock:
            return False
        now_mono = time.monotonic()
        if self._static_since <= 0.0:
            self._static_since = now_mono
            return self._person_present
        timeout = self._proactive.num('static_person_timeout_sec', 600)
        if self._person_present and now_mono - self._static_since < timeout:
            return True                     # 在场且静止未超时：先信（真人也可能久坐）
        verdict = self._vlm_person_check()
        if verdict is None:
            return self._person_present     # 仲裁不可用：保守维持现状
        if verdict:
            self._static_since = now_mono   # 真人但安静：重新计时继续观察
            return True
        self._static_lock = True
        self._static_since = 0.0
        return False

    def _vlm_person_check(self) -> bool | None:
        """调 camera-tool.py person 让 VLM 判断画面里是否真人。

        消耗闸（2026-11-04 发起人问模型消耗后加）：60 秒限频 + 当日预算
        （person_check_daily_cap，默认 150 次）；超预算/失败返回 None，
        门控保守维持现状，绝不因省额度把真人判没。
        """
        today = time.strftime('%Y-%m-%d')
        if today != self._person_checks_date:
            self._person_checks_date = today
            self._person_checks_today = 0
        if self._person_checks_today >= max(1, int(self._proactive.num('person_check_daily_cap', 150))):
            return None
        now_mono = time.monotonic()
        if now_mono - self._person_check_at < 60.0:
            return None
        self._person_check_at = now_mono
        self._person_checks_today += 1
        import subprocess
        tool = Path(r'F:\coding\guanjia-v2-development\openclaw\workspace\tools\camera-tool.py')
        try:
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', str(tool), 'person'],
                capture_output=True, text=True, timeout=45,
            )
        except Exception:
            return None
        for line in reversed((result.stdout or '').splitlines()):
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data.get('person'), bool):
                return data['person']
            return None
        return None

    def request_standby(self, pan: float | None) -> None:
        """进入卧室待命（本地规则）：镜头朝卧室门口等离开沿，期间不说不跟。

        pan=None 时使用 proactive.json 的 bedroom_standby_pan（未校准则忽略）。
        触发信号源（"人进卧室"的判定）由主动环触发引擎接线，本方法只管朝向状态机。
        """
        target = pan if pan is not None else self._proactive.opt_num('bedroom_standby_pan')
        if target is None:
            return
        self.mode = 'standby'
        self._standby_target_pan = float(target)
        self._standby_since = time.monotonic()

    def release_standby(self) -> None:
        """离开沿确认有人出来后回巡逻模式（夜间静默由时钟判定，独立于本状态）。"""
        self.mode = 'patrol'
        self._standby_target_pan = None

    def _is_night(self, now_local: datetime | None = None) -> bool:
        local = (now_local or datetime.now()).hour
        return is_night_hour(local, self._proactive.night_start_hour, self._proactive.night_end_hour)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = threading.Event()  # 新一轮运行用全新的事件，保证 stop 后可重新 start
        self._thread = threading.Thread(target=self._run, name='vision-loop', daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def latest_context(self) -> dict[str, object]:
        try:
            return json.loads(self.latest_path.read_text(encoding='utf-8'))
        except Exception:
            return {'ok': False, 'reason': 'no_vision_frame_yet'}

    def capture_once(self) -> dict[str, object]:
        raw = self._snapshot_bytes()
        image = Image.open(BytesIO(raw)).convert('RGB')
        detected, confidence, box_norm = self._detector.detect(image)
        now_local = datetime.now()
        night = self._is_night(now_local)
        standby = self.mode == 'standby'
        # 离开沿后 N 分钟（深度降级状态窗，议会遗留调参 leave_window_min）
        deep = night or standby or time.monotonic() < self._leave_deep_until
        # ---- 隐私降级·采集源头生效：原图只在内存，落盘/外发均为降级版 ----
        box_px = self._box_to_pixels(box_norm, image.width, image.height) if box_norm else None
        processed = self._downscaler.apply(image, box_px, deep=deep)
        privacy_level = 2 if deep else 1
        small = processed.convert('L').resize((160, 90))
        change_score = self._change_score(small)
        if self._baseline_reset:  # PTZ 自转排除：转动后首帧只重置基线，不报变化
            change_score = 0.0
            self._baseline_reset = False
            self._last_small = None
        changed = change_score >= self.config.change_threshold
        # 误检治理：静止场景的 YOLO 命中先过动静门 + VLM 仲裁，才允许进入在场状态机
        effective_detected = self._person_gate(detected, changed, change_score)
        person_entered, person_left = self._update_person_state(effective_detected)
        if person_left:  # 离开沿 → 开深度降级窗（下一次有人前，画面里的残留帧也算敏感期）
            self._leave_deep_until = time.monotonic() + self._proactive.num('leave_window_min', 10) * 60
        interval = (
            self.config.person_interval_sec if effective_detected or self._person_present
            else self.config.changed_interval_sec if changed
            else self.config.normal_interval_sec
        )
        now = datetime.now(timezone.utc)
        frame_id = now.strftime('%Y%m%dT%H%M%S.%fZ')
        image_path = self.frames_dir / f'{frame_id}.jpg'
        processed.save(image_path, format='JPEG', quality=82)
        position = self._position()
        record = {
            'ok': True,
            'ts': now.isoformat(),
            'frame_id': frame_id,
            'image_path': str(image_path),
            'change_score': round(change_score, 4),
            'changed': changed,
            'person_present': self._person_present,
            'person_confidence': round(confidence, 4),
            'person_detected_raw': detected,        # YOLO 原始命中（误检治理前的值，排障用）
            'person_static_lock': self._static_lock,  # 静物锁定中（玩偶/抱枕误检已隔离）
            'person_entered': person_entered,
            'person_left': person_left,
            'person_box': self._round_box(box_norm),
            'camera_position': position,
            'area': self._area(position),
            'next_interval_sec': interval,
            'camera_mode': 'night' if night else self.mode,
            'privacy_level': privacy_level,
            'privacy_flagged': privacy_level >= 2,
            'send_to_model': not (night or standby),  # 待命/夜间帧不送层
            'summary': self._summary(changed, person_entered),
        }
        self._last_small = small
        self._record(record)
        self._write_json(self.latest_path, record)
        self._prune(now)
        # ---- 沿事件分发（record 就绪后）：A 身份感知等主动环消费者接线用 ----
        if (person_entered or person_left) and callable(self.on_edge):
            try:
                self.on_edge(record, 'entered' if person_entered else 'left')
            except Exception:
                pass  # 沿回调失败绝不拖垮视觉循环
        # ---- 独处计时（发起人定 1 小时）：人持续在场无沿 → 模拟 entered 沿 ----
        # 反方评审指出的核心缺口：人坐3小时不动永远没有沿 → 管线永远不触发。
        # 现在持续在场满 1 小时后模拟一次 entered 沿给管线；离场重置计时。
        alone_sec = self.config.num('alone_timer_sec', 3600.0) if hasattr(self.config, 'num') else 3600.0
        now_mono = time.monotonic()
        if self._person_present:
            if self._alone_since <= 0.0:
                self._alone_since = now_mono
            elif not self._alone_edge_fired and (now_mono - self._alone_since) >= alone_sec:
                self._alone_edge_fired = True
                record['alone_elapsed_sec'] = round(now_mono - self._alone_since, 0)
                if callable(self.on_edge):
                    try:
                        self.on_edge(record, 'alone')
                    except Exception:
                        pass  # 独处沿失败绝不拖垮视觉循环
        else:
            self._alone_since = 0.0
            self._alone_edge_fired = False
        # ---- 每帧轻 tick（V3）：在场身份 TTL 续期等无阻塞消费者 ----
        if callable(self.on_tick):
            try:
                self.on_tick(record)
            except Exception:
                pass  # tick 失败绝不拖垮视觉循环
        # ---- 本地跟转（patrol 模式、非夜间、人在边缘）+ 待命朝向保持 ----
        # 用门控后的有效检测：静止误检（玩偶）绝不能带着镜头追假人
        self._steer(effective_detected, box_norm, night, now_local)
        return record

    @staticmethod
    def _box_to_pixels(box: tuple[float, float, float, float], width: int, height: int) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = box
        return int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height)

    @staticmethod
    def _round_box(box: tuple[float, float, float, float] | None) -> list[float] | None:
        if box is None:
            return None
        return [round(v, 4) for v in box]

    def _steer(self, detected: bool, box: tuple[float, float, float, float] | None,
               night: bool, now_local: datetime) -> None:
        """本地转向控制：待命朝向保持 + 边缘跟转；转动期间停稳再拍（自转排除）。"""
        if self._ptz is None or time.monotonic() < self._settle_until:
            return
        if self.mode == 'standby' and self._standby_target_pan is not None:
            # 待命超时回巡逻（人可能从别的门走了）
            timeout = self._proactive.num('bedroom_standby_timeout_min', 120) * 60
            if time.monotonic() - self._standby_since > timeout:
                self.release_standby()
                return
            current = float(self._position().get('pan') or 0.0)
            if abs(self._standby_target_pan - current) > self._proactive.num('standby_pan_tolerance', 0.05):
                self._move_toward(self._standby_target_pan, current)
            return
        if night or not detected or box is None:
            # 无人时回看护位（发起人反馈：摄像头一直对着墙——
            # 老标定把正对墙/电视特写的位置记成了 pan=0，回中反而永远面壁）：
            # 目标改为 proactive.json 的 home_pan（2026-10-27 实拍校准 0.311，能看到电视/过道/冰箱）
            if not detected and not night and self._person_present is False:
                # 护位=沙发方向 + 下俯（2026-11-03 发起人实拍两轮定稿：0.6 / -0.35）
                home_pan = float(self._proactive.num('home_pan', 0.6))
                home_tilt = float(self._proactive.num('home_tilt', -0.35))
                position = self._position()
                current_pan = float(position.get('pan') or 0.0)
                current_tilt = float(position.get('tilt') or 0.0)
                off_target = (abs(current_pan - home_pan) > 0.08
                              or abs(current_tilt - home_tilt) > 0.08)
                if off_target and time.monotonic() > self._track_cooldown_until:
                    self._move_toward(home_pan, current_pan, home_tilt, current_tilt)
            return  # 夜间不转；无人/无框不跟
        center_x = (box[0] + box[2]) / 2.0
        edge = self._proactive.num('track_edge', 0.22)
        if time.monotonic() < self._track_cooldown_until:
            return
        if center_x < edge:
            self._move_step(-1.0)   # 人在左缘，镜头往左追
        elif center_x > 1.0 - edge:
            self._move_step(1.0)    # 人在右缘，镜头往右追

    def _move_step(self, horizontal: float) -> None:
        """小步跟转 + 切换迟滞：设 settle 窗（停稳再拍）与冷却窗（防来回摆动）。"""
        duration = self._proactive.num('track_step_duration', 0.25)
        try:
            self._ptz.move(horizontal, 0.0, duration)
        except Exception:
            return
        self._moves_since_anchor += 1  # 转动积分是位置漂移的唯一来源，累计后触发重锚
        self._settle_until = time.monotonic() + self._proactive.num('settle_sec', 1.6)
        self._baseline_reset = True
        self._track_cooldown_until = time.monotonic() + self._proactive.num('track_cooldown_sec', 2.0)

    def _move_toward(self, target_pan: float, current_pan: float,
                     target_tilt: float | None = None,
                     current_tilt: float | None = None) -> None:
        """朝目标归位（护位/待命）：pan、tilt 分两次单轴小步，逐步逼近。

        本机合并斜转（x、y 同时非零）实测不按预期执行（2026-11-03），必须单轴分开转。
        """
        delta = target_pan - current_pan
        if abs(delta) > 0.01:
            direction = 1.0 if delta > 0 else -1.0
            duration = min(0.5, max(0.08, abs(delta) * 0.3))
            try:
                self._ptz.move(direction, 0.0, duration)
            except Exception:
                return
            self._moves_since_anchor += 1
            self._settle_until = time.monotonic() + self._proactive.num('settle_sec', 1.6)
            self._baseline_reset = True
        if target_tilt is None or current_tilt is None:
            return
        delta_tilt = target_tilt - current_tilt
        if abs(delta_tilt) > 0.01:
            direction_tilt = 1.0 if delta_tilt > 0 else -1.0
            duration_tilt = min(0.5, max(0.08, abs(delta_tilt) * 0.3))
            try:
                self._ptz.move(0.0, direction_tilt, duration_tilt)
            except Exception:
                return
            self._moves_since_anchor += 1
            self._settle_until = time.monotonic() + self._proactive.num('settle_sec', 1.6)
            self._baseline_reset = True

    def _run(self) -> None:
        interval = 0.0
        while not self._stop.wait(interval):
            if time.monotonic() < self._settle_until:
                interval = 0.3  # 转动中不抓帧（停稳再拍）
                continue
            self._maybe_reanchor()
            try:
                interval = float(self.capture_once()['next_interval_sec'])
                self._write_json(self.health_path, {
                    'ok': True, 'ts': datetime.now(timezone.utc).isoformat(),
                    # 模型消耗审计（2026-11-04 发起人关注）：当日真人仲裁次数 + 距下次重锚的转动步数
                    'vlm_person_checks_today': self._person_checks_today,
                    'ptz_moves_since_anchor': self._moves_since_anchor,
                })
            except Exception as exc:  # noqa: BLE001
                self._write_json(self.health_path, {
                    'ok': False,
                    'ts': datetime.now(timezone.utc).isoformat(),
                    'error': f'{type(exc).__name__}: {exc}',
                })
                interval = self.config.normal_interval_sec

    def _maybe_reanchor(self) -> None:
        """按需重锚（省额度版）：位置漂移只来自转动指令的积分误差，
        所以累计转过 ≥reanchor_move_threshold 步才重锚，12 小时兜底一次；
        没转镜头的日子 0 次调用。锚点桶粗（±0.15），小偏差不采信，大脱节（≥0.2）才纠。
        """
        if self._ptz is None or self._person_present:
            return
        threshold = max(1, int(self._proactive.num('reanchor_move_threshold', 15)))
        due = (self._moves_since_anchor >= threshold
               or time.monotonic() >= self._reanchor_at)
        if not due:
            return
        try:
            anchor = self._vlm_reanchor()
        except Exception:
            anchor = None
        if anchor is None:
            # 失败退避：10 分钟后再试，绝不每帧重试打爆调用
            self._reanchor_at = time.monotonic() + 600.0
            return
        self._moves_since_anchor = 0
        self._reanchor_at = time.monotonic() + max(
            3600.0, self._proactive.num('reanchor_interval_sec', 43200))
        try:
            current = float(self._position().get('pan') or 0.0)
            if abs(anchor - current) >= 0.2:
                self._ptz.set_position(pan=anchor)
        except Exception:
            pass  # 采信失败不影响视觉循环

    def _position(self) -> dict[str, object]:
        """Read the software position maintained by LocalCameraPtzAdapter."""
        try:
            state = json.loads(self.position_path.read_text(encoding='utf-8'))
            return {
                'pan': round(float(state.get('pan') or 0.0), 3),
                'tilt': round(float(state.get('tilt') or 0.0), 3),
                'calibrated': bool(state.get('calibrated')),
            }
        except Exception:
            return {'pan': 0.0, 'tilt': 0.0, 'calibrated': False}

    def _area(self, position: dict[str, object]) -> str:
        pan = position.get('pan')
        if not position.get('calibrated') or pan is None:
            return ''
        if self._room_zones is None:
            try:
                data = json.loads(self.room_map_path.read_text(encoding='utf-8'))
                self._room_zones = list(data.get('zones') or [])
            except Exception:
                self._room_zones = []
        return area_for_pan(float(pan), self._room_zones)

    def _snapshot_bytes(self) -> bytes:
        cfg = self._camera
        uri = f'http://{cfg.ip}:{cfg.http_port}/onvifsnapshot/media_service/snapshot?channel=1&subtype=0'
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, f'http://{cfg.ip}/', cfg.username, cfg.password)
        manager.add_password(None, uri, cfg.username, cfg.password)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPBasicAuthHandler(manager),
        )
        with opener.open(uri, timeout=4.0) as response:
            return response.read()

    def _change_score(self, image: Image.Image) -> float:
        if self._last_small is None:
            return 0.0
        histogram = ImageChops.difference(image, self._last_small).histogram()
        return sum(value * count for value, count in enumerate(histogram)) / (
            255 * image.width * image.height
        )

    def _update_person_state(self, detected: bool) -> tuple[bool, bool]:
        """更新在场状态；返回 (进入沿, 离开沿)。"""
        previous = self._person_present
        if detected:
            self._positive_hits += 1
            self._negative_hits = 0
            if self._positive_hits >= 2:
                self._person_present = True
        else:
            self._negative_hits += 1
            self._positive_hits = 0
            if self._negative_hits >= 3:
                self._person_present = False
        entered = self._person_present and not previous
        left = (not self._person_present) and previous
        if entered and self.mode == 'standby':
            # V7b 修复：待命期看到人出现（=人从卧室出来走进画面）→ 回巡逻跟转。
            # 原实现错挂在 left 沿上（待命期画面本来就无人，left 永不触发，
            # 待命只能靠 120 分钟超时解除）。
            self.release_standby()
        return entered, left

    def _summary(self, changed: bool, entered: bool) -> str:
        if entered:
            return '检测到有人进入画面'
        if self._person_present:
            return '画面中有人'
        if changed:
            return '画面有明显变化，未检测到人'
        return '画面变化很小，未检测到人'

    def _init_db(self) -> None:
        """frames 时序表：主动环 E 组件（习惯学习）的原料。旧库自动补新列。"""
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS frames ('
                'frame_id TEXT PRIMARY KEY, ts TEXT NOT NULL, image_path TEXT NOT NULL, '
                'size_bytes INTEGER NOT NULL, changed INTEGER NOT NULL, change_score REAL NOT NULL, '
                'person_present INTEGER NOT NULL, person_confidence REAL NOT NULL, person_entered INTEGER NOT NULL)'
            )
            for column, ddl in (
                ('pan', 'REAL'),
                ('area', 'TEXT'),
                ('camera_mode', 'TEXT'),
                ('privacy_level', 'INTEGER DEFAULT 1'),
                ('person_left', 'INTEGER DEFAULT 0'),
                ('send_to_model', 'INTEGER DEFAULT 1'),
            ):
                try:
                    db.execute(f'ALTER TABLE frames ADD COLUMN {column} {ddl}')
                except sqlite3.OperationalError:
                    pass  # 列已存在

    def _record(self, record: dict[str, object]) -> None:
        path = Path(str(record['image_path']))
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                'INSERT INTO frames (frame_id, ts, image_path, size_bytes, changed, change_score, '
                'person_present, person_confidence, person_entered, pan, area, camera_mode, '
                'privacy_level, person_left, send_to_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    record['frame_id'], record['ts'], str(path), path.stat().st_size,
                    int(bool(record['changed'])), record['change_score'],
                    int(bool(record['person_present'])), record['person_confidence'],
                    int(bool(record['person_entered'])),
                    float((record.get('camera_position') or {}).get('pan') or 0.0),
                    str(record.get('area') or ''),
                    str(record.get('camera_mode') or 'patrol'),
                    int(record.get('privacy_level') or 1),
                    int(bool(record.get('person_left'))),
                    int(bool(record.get('send_to_model', True))),
                ),
            )

    def _prune(self, now: datetime) -> None:
        cutoff = (now - timedelta(hours=self.config.max_hours)).isoformat()
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute(
                'SELECT frame_id, image_path FROM frames WHERE ts < ? ORDER BY ts',
                (cutoff,),
            ).fetchall()
            self._delete_rows(db, rows)
            count, size = db.execute('SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM frames').fetchone()
            while count > self.config.max_frames or size > self.config.max_bytes:
                row = db.execute(
                    'SELECT frame_id, image_path FROM frames '
                    'ORDER BY person_entered ASC, person_present ASC, changed ASC, ts ASC LIMIT 1'
                ).fetchone()
                if row is None:
                    break
                self._delete_rows(db, [row])
                count, size = db.execute(
                    'SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM frames'
                ).fetchone()

    @staticmethod
    def _delete_rows(db: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
        for frame_id, image_path in rows:
            try:
                Path(image_path).unlink(missing_ok=True)
            finally:
                db.execute('DELETE FROM frames WHERE frame_id = ?', (frame_id,))

    @staticmethod
    def _write_json(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)


__all__ = ['PersonDetector', 'VisionLoop', 'VisionLoopConfig', 'area_for_pan', 'vision_note']