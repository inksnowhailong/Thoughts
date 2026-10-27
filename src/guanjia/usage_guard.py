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

    def record(self, usage: dict | None, model: str | None = None) -> None:
        """每轮主脑调用后累计（usage 含 input/output/cacheRead 字段）。

        model 参数用于区分付费模型（glm-5.3 完整版无免费额度——真金白银）。
        付费模型有独立的日耗上限：超过 glm53_daily_cap_yuan 后告警，
        超过两倍后自动退回 flash（防止烧光余额）。
        """
        if not isinstance(usage, dict) or not usage:
            return
        gained = {
            'input': int(usage.get('input') or 0),
            'output': int(usage.get('output') or 0),
            'cache_read': int(usage.get('cacheRead') or 0),
            'calls': 1,
        }
        is_paid = isinstance(model, str) and 'flash' not in model.lower() and model.strip()
        with self._lock:
            self._roll_day()
            for key, value in gained.items():
                self._state[key] = self._state.get(key, 0) + value
            if is_paid:
                self._state['paid_calls'] = self._state.get('paid_calls', 0) + 1
                self._state['paid_input'] = self._state.get('paid_input', 0) + gained['input']
                self._state['paid_output'] = self._state.get('paid_output', 0) + gained['output']
                self._state['paid_model'] = model
                self._check_paid_alert()
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

    def _check_paid_alert(self) -> None:
        """付费模型（无免费额度的 glm-5.3 等）的成本防线。

        输入 8元/百万 + 输出 28元/百万 → 估算日耗金额。
        超过 glm53_daily_cap_yuan（默认 2 元）→ 告警；
        超过两倍 → 自动关闭游戏模式模型升档（回 flash），当天不再用付费模型。
        """
        # 估算成本（元）：输入 8/M + 输出 28/M
        cost = (self._state.get('paid_input', 0) * 8 + self._state.get('paid_output', 0) * 28) / 1_000_000
        cap = self._hub.num('limits.glm53_daily_cap_yuan', 2.0)
        if cost >= cap and not self._state.get('paid_alert'):
            self._state['paid_alert'] = f'付费模型日耗已到 {cost:.2f} 元（防线 {cap} 元）'
        if cost >= cap * 2 and not self._state.get('paid_disabled'):
            self._state['paid_disabled'] = True
            self._state['paid_disabled_at_cost'] = round(cost, 2)
            # 退回 flash：写 voice.env 让 GUANJIA_VOICE_MODEL 为空（走默认 flash）
            try:
                env_path = _REPO / 'tmp' / 'voice.env'
                env_path.parent.mkdir(parents=True, exist_ok=True)
                lines = env_path.read_text(encoding='utf-8').splitlines() if env_path.exists() else []
                filtered = [l for l in lines if not l.startswith('GUANJIA_GAMING_MODEL=')]
                filtered.append('GUANJIA_GAMING_MODEL=flash')
                env_path.write_text('\n'.join(filtered) + '\n', encoding='utf-8')
            except Exception:
                pass

    def is_paid_disabled(self) -> bool:
        """付费模型是否已被自动关闭（游戏模式模型升档被熔断）。"""
        with self._lock:
            self._roll_day()
            return bool(self._state.get('paid_disabled'))

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
                data = _json.loads(p.read_text(encoding='utf-8-sig'))  # 兼容 BOM 头，否则静默关停失效
                data['proactive_enabled'] = False
                p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                self._state['note'] = '主动环已自动关停（proactive_enabled=false），被动对话照常'
            except Exception:
                pass


__all__ = ['UsageGuard']
