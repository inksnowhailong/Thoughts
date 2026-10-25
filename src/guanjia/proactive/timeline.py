"""时间轴系统（发起人裁决 7）：重要时段预热预设，沿时刻只填动态位。

混合时序（议会定稿 §1-C/D）：
- 预热：安静期（每小时检查一次习惯库）在重要时间段前生成预设句存出箱，
  当日有效、过期即弃（QC-12：TTL 纳入 freshness 语义——新一天自动清箱）
- 沿时刻：trigger 管线评审通过后，若当日箱有匹配预设则直接填空出声，省一次现场生成
  （沿时刻仍过评审，预设不免审——SX-8）；难预测时刻（箱空）临时现想

token 账本：预热 3~5 次/日（JG-21 测算 b 项）；预设未命中即作废，作废率入测算。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
import threading
import time
from pathlib import Path

import httpx

from guanjia.brain import load_gateway_token
from guanjia.proactive.config import ProactiveConfig
from guanjia.proactive.habits import HabitStore

_WARM_SESSION = 'agent:main:proactive-warm'
_GATEWAY_URL = 'http://127.0.0.1:18789/v1/chat/completions'

_WARM_PROMPT = (
    '你是家庭管家。根据这个作息习惯：{fact}，'
    '预想一句在家人到家/出现时主动说的欢迎话。只输出要直接说出口的口语：'
    '每句不超过 30 个字、最多两句、无 Markdown、无表情。'
    '不要写具体时间数字（到家时再补）。直接输出那句话。'
)


class Timeline:
    """当日预设箱 + 每小时习惯检查的预热线程。"""

    def __init__(
        self,
        config: ProactiveConfig | None = None,
        habits: HabitStore | None = None,
        box_path: Path | None = None,
    ) -> None:
        self._config = config or ProactiveConfig()
        self._habits = habits or HabitStore(
            Path(str(self._config.path)).parent.parent / 'habits.sqlite')
        self._box_path = box_path or (
            Path(str(self._config.path)).parent / 'timeline-presets.json')
        self._token = load_gateway_token()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warmed_keys: set[str] = set()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='proactive-timeline', daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        """每小时醒一次：清过期（非当日）预设，检查习惯窗，预热未备的时段。"""
        while not self._stop.wait(3600.0):
            try:
                self.prune_expired()
                if not self._enabled():
                    continue
                now = datetime.now()
                for habit in self._habits.habits_around(
                        (now.hour + 1) % 24, 0,
                        window_min=int(self._config.num('warm_window_min', 30))):
                    self._warm(habit)
            except Exception:
                pass  # 预热线程永不致命

    # ---------- 预设箱 ----------

    def preset_for(self, who: str, hour: int) -> str | None:
        """沿时刻取预设：命中当日箱返回文本（trigger 填动态位用），未命中返回 None（现想）。"""
        box = self._load_box()
        today = datetime.now().strftime('%Y-%m-%d')
        for item in box:
            if str(item.get('date')) != today:
                continue
            box_who = str(item.get('who') or '')
            # V2 修复：习惯提取写的是泛化名"家人"，匹配任何在场身份；
            # 只有明确的个人名（主人/女主人）才做精确匹配
            if box_who not in ('', who) and box_who not in ('家人', '任意'):
                continue
            try:
                if abs(int(item.get('hour', -1)) - hour) <= int(
                        self._config.num('preset_match_hour_window', 2)):
                    return str(item.get('text') or '') or None
            except (TypeError, ValueError):
                continue
        return None

    def prune_expired(self) -> None:
        """当日有效（QC-12）：清掉非今天的预设。"""
        box = self._load_box()
        today = datetime.now().strftime('%Y-%m-%d')
        kept = [item for item in box if str(item.get('date')) == today]
        if len(kept) != len(box):
            self._save_box(kept)

    # ---------- 预热 ----------

    def _warm(self, habit: dict[str, object]) -> None:
        # V5 修复：去重带日期——进程常驻时第二天同时段重新预热
        key = str(habit.get('key') or '')
        warm_key = f"{datetime.now().strftime('%Y-%m-%d')}:{key}"
        if not key or warm_key in self._warmed_keys:
            return
        fact = str(habit.get('fact') or '').strip()
        if not fact:
            return
        reply = self._ask_gateway(_WARM_PROMPT.format(fact=fact))
        text = re.sub(r'\s+', ' ', (reply or '')).strip()[:120]
        if not text:
            return
        box = self._load_box()
        box.append({
            'date': datetime.now().strftime('%Y-%m-%d'),
            'hour': int(str(habit.get('time_key') or '0').split(':')[0] or 0),
            'who': str(habit.get('who') or ''),
            'area': str(habit.get('area') or ''),
            'text': text,
            'warmed_at': datetime.now().isoformat(timespec='seconds'),
        })
        self._save_box(box)
        self._warmed_keys.add(warm_key)

    def _enabled(self) -> bool:
        """全局开关热重读（V4 修复）：预热线程每次检查重读 proactive.json。"""
        try:
            self._config = ProactiveConfig(path=self._config.path)
        except Exception:
            pass
        return bool(self._config.values.get('proactive_enabled', True))

    def _load_box(self) -> list[dict[str, object]]:
        try:
            data = json.loads(self._box_path.read_text(encoding='utf-8'))
            return list(data) if isinstance(data, list) else []
        except Exception:
            return []

    def _save_box(self, box: list[dict[str, object]]) -> None:
        try:
            self._box_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._box_path.with_suffix('.tmp')
            temporary.write_text(
                json.dumps(box, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self._box_path)
        except Exception:
            pass

    def _ask_gateway(self, prompt: str) -> str | None:
        """预热生成：轻思考 low + 采样多样性（口语自然句），非确定性输出。"""
        body = {
            'model': 'openclaw',
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': False,
            'thinking': {'type': 'enabled'},   # flash 强制项
            'reasoning_effort': 'low',         # 预设句生成不需深思考
            'do_sample': True,                 # 采样多样性，每次预设不千篇一律
        }
        headers = {
            'Authorization': f'Bearer {self._token}',
            'Content-Type': 'application/json',
            'x-openclaw-session-key': _WARM_SESSION,
            'x-openclaw-agent-id': 'main',  # 多 agent 架构：网关要求显式指定，缺头会被 400 拒掉
        }
        try:
            with httpx.Client(timeout=30.0, trust_env=False) as client:
                response = client.post(_GATEWAY_URL, headers=headers, json=body)
                response.raise_for_status()
                return str(response.json()['choices'][0]['message']['content'])
        except Exception:
            return None


__all__ = ['Timeline']
