"""UsageGuard：主脑日耗 token 防线（发起人要求：防止余额被主脑烧掉）。

背景：glm-5.3-flash 的 1960 万免费 tokens 是资源包，耗尽后自动转按量计费
烧账户余额（余额现用于 TTS）。本防线累计每日真实消耗（从 gateway.run 的
usage 事件），超过阈值告警——数据给主人看，防线由配置 limits.daily_token_alert 定。

不自动关停主脑（那会瘫掉管家），告警 + 记账；超阈值倍数时停主动环（可自动恢复）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from guanjia.config_hub import ConfigHub

_REPO = Path(__import__('os').environ.get('GUANJIA_ROOT', r'F:\coding\guanjia-v2-development'))
_USAGE_DIR = _REPO / 'data' / 'usage'


class UsageGuard:
    """线程安全；voice 进程内单例，挂在 gateway.run 的 usage 处。"""

    def __init__(self, hub: ConfigHub | None = None) -> None:
        self._hub = hub or ConfigHub()
        self._lock = threading.Lock()
        self._today = ''
        self._state: dict = {}
        self._alerted = False
        self._proactive_disabled = False

    # ---------- 对外 ----------

    def record(self, usage: dict | None) -> None:
        """每轮主脑调用后累计（usage 含 input/output/cacheRead 字段）。"""
        if not isinstance(usage, dict) or not usage:
            return
        gained = {
            'input': int(usage.get('input') or 0),
            'output': int(usage.get('output') or 0),
            'cache_read': int(usage.get('cacheRead') or 0),
            'calls': 1,
        }
        with self._lock:
            self._roll_day()
            for key, value in gained.items():
                self._state[key] = self._state.get(key, 0) + value
            self._persist()
            self._check_alerts()

    def summary(self) -> dict:
        """当日账单（面板/主人查看用）。"""
        with self._lock:
            self._roll_day()
            return dict(self._state)

    # ---------- 内部 ----------

    def _roll_day(self) -> None:
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self._today:
            self._today = today
            self._state = {'date': today, 'input': 0, 'output': 0, 'cache_read': 0, 'calls': 0}
            self._alerted = False
            self._proactive_disabled = False

    def _persist(self) -> None:
        try:
            _USAGE_DIR.mkdir(parents=True, exist_ok=True)
            path = _USAGE_DIR / f'daily-{self._today}.json'
            payload = dict(self._state)
            payload['alert_threshold'] = self._threshold()
            payload['proactive_disabled'] = self._proactive_disabled
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _threshold(self) -> int:
        return int(self._hub.num('limits.daily_token_alert', 500000))

    def _check_alerts(self) -> None:
        total = self._state.get('input', 0) + self._state.get('output', 0) + self._state.get('cache_read', 0)
        threshold = self._threshold()
        if total >= threshold and not self._alerted:
            self._alerted = True
            self._state['alert'] = (
                f'日耗 {total} tokens 超过防线 {threshold}——若免费资源包已耗尽，'
                '此刻开始烧账户余额；请到智谱控制台核对资源包余量')
        if total >= threshold * 2 and not self._proactive_disabled:
            # 双倍阈值：停主动环（配置文件开关，热生效），被动对话不受影响
            self._proactive_disabled = True
            try:
                import json as _json
                p = _REPO / 'data' / 'vision' / 'proactive.json'
                data = _json.loads(p.read_text(encoding='utf-8'))
                data['proactive_enabled'] = False
                p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                self._state['note'] = '主动环已自动关停（proactive_enabled=false），被动对话照常'
            except Exception:
                pass


__all__ = ['UsageGuard']
