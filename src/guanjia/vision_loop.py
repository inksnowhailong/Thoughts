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
import urllib.request

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageChops

from guanjia.output.adapters.camera_ptz import LocalCameraConfig


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

    def detect(self, image: Image.Image) -> tuple[bool, float]:
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
            return False, 0.0
        if output.shape[0] < output.shape[1] and output.shape[0] <= 100:
            output = output.T
        if output.shape[1] == 84:  # YOLOv8: box + 80 class scores; column 4 is the person class
            scores = output[:, 4]
        elif output.shape[1] >= 85:  # YOLOv5-style: box + objectness + classes
            scores = output[:, 4] * output[:, 5]
        else:
            return False, 0.0
        score = float(np.max(scores)) if scores.size else 0.0
        return score >= self._confidence, score


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
        self._init_db()

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
        small = image.convert('L').resize((160, 90))
        change_score = self._change_score(small)
        changed = change_score >= self.config.change_threshold
        detected, confidence = self._detector.detect(image)
        person_entered = self._update_person_state(detected)
        interval = (
            self.config.person_interval_sec if detected or self._person_present
            else self.config.changed_interval_sec if changed
            else self.config.normal_interval_sec
        )
        now = datetime.now(timezone.utc)
        frame_id = now.strftime('%Y%m%dT%H%M%S.%fZ')
        image_path = self.frames_dir / f'{frame_id}.jpg'
        image.save(image_path, format='JPEG', quality=82)
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
            'camera_position': position,
            'area': self._area(position),
            'next_interval_sec': interval,
            'summary': self._summary(changed, person_entered),
        }
        self._last_small = small
        self._record(record)
        self._write_json(self.latest_path, record)
        self._prune(now)
        return record

    def _run(self) -> None:
        interval = 0.0
        while not self._stop.wait(interval):
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

    def _update_person_state(self, detected: bool) -> bool:
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
        return self._person_present and not previous

    def _summary(self, changed: bool, entered: bool) -> str:
        if entered:
            return '检测到有人进入画面'
        if self._person_present:
            return '画面中有人'
        if changed:
            return '画面有明显变化，未检测到人'
        return '画面变化很小，未检测到人'

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS frames ('
                'frame_id TEXT PRIMARY KEY, ts TEXT NOT NULL, image_path TEXT NOT NULL, '
                'size_bytes INTEGER NOT NULL, changed INTEGER NOT NULL, change_score REAL NOT NULL, '
                'person_present INTEGER NOT NULL, person_confidence REAL NOT NULL, person_entered INTEGER NOT NULL)'
            )

    def _record(self, record: dict[str, object]) -> None:
        path = Path(str(record['image_path']))
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                'INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    record['frame_id'], record['ts'], str(path), path.stat().st_size,
                    int(bool(record['changed'])), record['change_score'],
                    int(bool(record['person_present'])), record['person_confidence'],
                    int(bool(record['person_entered'])),
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