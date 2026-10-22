"""Passive camera loop: adaptive capture, change score, person detection, position-aware context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
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
        self.on_edge: object | None = None        # 沿回调钩子：fn(record, edge) edge∈{entered,left}——A 身份感知接线用
        self.on_tick: object | None = None        # 每帧轻回调：fn(record)——在场身份 TTL 续期（V3）用；回调必须无阻塞
        self._init_db()

    def _init_ptz(self) -> LocalCameraPtzAdapter | None:
        """PTZ 惰性初始化；相机不在/ONVIF 不通时视觉循环照常拍照，只是不转。"""
        try:
            driver = OnvifPtzDriver(self._camera)
            return LocalCameraPtzAdapter(driver)
        except Exception:
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
        person_entered, person_left = self._update_person_state(detected)
        if person_left:  # 离开沿 → 开深度降级窗（下一次有人前，画面里的残留帧也算敏感期）
            self._leave_deep_until = time.monotonic() + self._proactive.num('leave_window_min', 10) * 60
        interval = (
            self.config.person_interval_sec if detected or self._person_present
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
        # ---- 每帧轻 tick（V3）：在场身份 TTL 续期等无阻塞消费者 ----
        if callable(self.on_tick):
            try:
                self.on_tick(record)
            except Exception:
                pass  # tick 失败绝不拖垮视觉循环
        # ---- 本地跟转（patrol 模式、非夜间、人在边缘）+ 待命朝向保持 ----
        self._steer(detected, box_norm, night, now_local)
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
        self._settle_until = time.monotonic() + self._proactive.num('settle_sec', 1.6)
        self._baseline_reset = True
        self._track_cooldown_until = time.monotonic() + self._proactive.num('track_cooldown_sec', 2.0)

    def _move_toward(self, target_pan: float, current_pan: float) -> None:
        """朝目标 pan 归位（待命朝向）：按软件位置差给方向小步，逐步逼近。"""
        delta = target_pan - current_pan
        direction = 1.0 if delta > 0 else -1.0
        duration = min(0.5, max(0.08, abs(delta) * 0.3))
        try:
            self._ptz.move(direction, 0.0, duration)
        except Exception:
            return
        self._settle_until = time.monotonic() + self._proactive.num('settle_sec', 1.6)
        self._baseline_reset = True

    def _run(self) -> None:
        interval = 0.0
        while not self._stop.wait(interval):
            if time.monotonic() < self._settle_until:
                interval = 0.3  # 转动中不抓帧（停稳再拍）
                continue
            try:
                interval = float(self.capture_once()['next_interval_sec'])
                self._write_json(self.health_path, {'ok': True, 'ts': datetime.now(timezone.utc).isoformat()})
            except Exception as exc:  # noqa: BLE001
                self._write_json(self.health_path, {
                    'ok': False,
                    'ts': datetime.now(timezone.utc).isoformat(),
                    'error': f'{type(exc).__name__}: {exc}',
                })
                interval = self.config.normal_interval_sec

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