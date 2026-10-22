"""E 记忆与作息·习惯库（议会定稿 SX-6 升级案）：结构化 upsert + 时间键直读。

落点（席位代裁定稿）：独立结构化库（sqlite，可 upsert 覆盖式更新，避免追加式 gems
积累"通常18点回家"与"最近19点到家"并存的矛盾条目）；时间键直读服务时间轴系统
（"当前时段谁通常在家"式精确查询，语义检索对这类查询偏弱）。

原料（QC-10 修正案执行前提）：vision_loop frames 时序表（pan/area/person 沿），
在园丁的安静窗口由 extract_vision_habits 统计成习惯观察。
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Iterator

_DB_DEFAULT = Path(r'F:\coding\guanjia-v2-development\data\habits.sqlite')


def _connect(path: Path) -> sqlite3.Connection:
    """打开习惯库连接并确保表结构；调用方用 with closing(_connect(...)) 包裹（Windows 句柄必关）。"""
    db = sqlite3.connect(str(path))
    db.execute(
        'CREATE TABLE IF NOT EXISTS habits ('
        'key TEXT PRIMARY KEY, who TEXT NOT NULL, kind TEXT NOT NULL, '
        'day_type TEXT NOT NULL, time_key TEXT NOT NULL, area TEXT DEFAULT "", '
        'fact TEXT NOT NULL, confidence REAL DEFAULT 0.3, observations INTEGER DEFAULT 1, '
        'updated_at TEXT NOT NULL, source TEXT DEFAULT "vision")'
    )
    return db


class HabitStore:
    """可 upsert 的结构化习惯库 + 时间键直读。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DB_DEFAULT
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def upsert(
        self,
        *,
        who: str,
        kind: str,
        day_type: str,
        time_key: str,
        fact: str,
        area: str = '',
        confidence: float = 0.3,
        source: str = 'vision',
    ) -> None:
        """覆盖式更新：同一 key 重复观察 → observations+1、confidence 递升、fact 以最新为准。"""
        key = f'{who}.{day_type}.{kind}.{time_key}' + (f'@{area}' if area else '')
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with closing(sqlite3.connect(str(self._path))) as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS habits ('
                'key TEXT PRIMARY KEY, who TEXT NOT NULL, kind TEXT NOT NULL, '
                'day_type TEXT NOT NULL, time_key TEXT NOT NULL, area TEXT DEFAULT "", '
                'fact TEXT NOT NULL, confidence REAL DEFAULT 0.3, observations INTEGER DEFAULT 1, '
                'updated_at TEXT NOT NULL, source TEXT DEFAULT "vision")'
            )
            row = db.execute(
                'SELECT observations, confidence FROM habits WHERE key=?', (key,)
            ).fetchone()
            if row is None:
                db.execute(
                    'INSERT INTO habits (key, who, kind, day_type, time_key, area, fact, '
                    'confidence, observations, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (key, who, kind, day_type, time_key, area, fact,
                     float(confidence), 1, now, source),
                )
            else:
                observations = int(row[0]) + 1
                # 置信上限 0.9：观察越多越可信，但永远保留被推翻的可能
                new_confidence = min(0.9, float(row[1]) + 0.1)
                db.execute(
                    'UPDATE habits SET fact=?, confidence=?, observations=?, updated_at=?, '
                    'source=? WHERE key=?',
                    (fact, new_confidence, observations, now, source, key),
                )
            db.commit()

    def habits_around(self, hour: int, minute: int = 0, *, who: str = '',
                      window_min: int = 45, day_type: str = '') -> list[dict[str, object]]:
        """时间键直读：返回 [现在-window, 现在+window] 内匹配的习惯（时间轴预热查询）。"""
        now_key = hour * 60 + minute
        results: list[dict[str, object]] = []
        try:
            with closing(_connect(self._path)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    'SELECT * FROM habits WHERE confidence >= 0.4 ORDER BY time_key'
                ).fetchall()
            for row in rows:
                parts = str(row['time_key']).split(':')
                try:
                    habit_key = int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
                except (ValueError, IndexError):
                    continue
                if abs(habit_key - now_key) <= window_min:
                    if who and str(row['who']) != who:
                        continue
                    if day_type and str(row['day_type']) not in (day_type, 'all'):
                        continue
                    results.append(dict(row))
        except Exception:
            return results
        return results

    def reset(self) -> None:
        """数据层安全网（JG-15/QC-13）：一键清库回冷启动态。"""
        with closing(_connect(self._path)) as db:
            db.execute('DELETE FROM habits')
            db.commit()


def extract_vision_habits(vision_db: Path, store: HabitStore, *,
                          min_observations: int = 3) -> int:
    """园丁安静窗口的视觉习惯提取（QC-10）：从 frames 时序统计人出现/消失的时段分布。

    第一版 area 级（不含身份维度——identity 沿数据积累后升级）：
    统计 person_entered/left 沿的小时分布，出现 >= min_observations 次的时段成习惯。
    """
    try:
        with closing(sqlite3.connect(str(vision_db))) as db:
            rows = db.execute(
                "SELECT ts, area, person_entered, person_left FROM frames "
                "WHERE person_entered=1 OR person_left=1 ORDER BY ts"
            ).fetchall()
    except Exception:
        return 0
    day_type = 'weekday' if datetime.now().weekday() < 5 else 'weekend'
    counts: dict[tuple[str, str, str], int] = {}
    for ts, area, entered, _left in rows:
        try:
            local = datetime.fromisoformat(str(ts)).astimezone()
        except (ValueError, TypeError):
            continue
        area = str(area or '').strip() or '未知区域'
        kind = 'person_present' if entered else 'person_absent'
        bucket = f'{local.hour:02d}:00'
        counts[(area, kind, bucket)] = counts.get((area, kind, bucket), 0) + 1
    written = 0
    for (area, kind, bucket), count in counts.items():
        if count < min_observations:
            continue
        who = '家人'
        fact = (
            f'{area}在 {bucket} 前后经常{"有人出现" if kind == "person_present" else "归于安静"}'
            f'（近 3 小时观察 {count} 次）'
        )
        store.upsert(who=who, kind=kind, day_type=day_type, time_key=bucket,
                     fact=fact, area=area, confidence=0.4)
        written += 1
    return written


__all__ = ['HabitStore', 'extract_vision_habits']
