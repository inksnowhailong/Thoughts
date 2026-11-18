"""主动环防线暂停（2026-11-06 事故）回归测试。

事故：10-26 一天 7.6M tokens（暴走日）触发 UsageGuard 双倍阈值，它把
data/vision/proactive.json 的 proactive_enabled 写成 false；注释说"可自动
恢复"，但恢复只重置内存标志、从不回写配置文件——主动环从此停摆 12 天，
主动对话一次都没再触发过。

修复：防线只做当天内存态暂停（跨天自动解除），配置文件里的
proactive_enabled 只反映主人的手工开关；ProactiveTrigger 经 pause_gate
接收防线状态。
"""
from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import guanjia.usage_guard as usage_guard_module
from guanjia.usage_guard import UsageGuard
from guanjia.proactive.trigger import ProactiveTrigger
from guanjia.proactive.config import ProactiveConfig


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def make_guard(tmp: Path) -> UsageGuard:
    guard = UsageGuard()
    usage_guard_module._USAGE_DIR = tmp / 'usage'          # 账单写进临时目录
    return guard


def test_guard_pause_is_ephemeral_and_never_touches_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / 'proactive.json'
        config_path.write_text(json.dumps({'proactive_enabled': True}), encoding='utf-8')
        guard = make_guard(tmp_path)

        # 日耗未超线：不暂停
        guard.record({'input': 100, 'output': 50, 'cacheRead': 0}, model='glm-5.3-flash')
        check(guard.proactive_paused() is False, 'normal usage must not pause the proactive ring')

        # 超双倍阈值（500k × 2 = 1M）：当天暂停
        guard.record({'input': 2_000_000, 'output': 0, 'cacheRead': 0}, model='glm-5.3-flash')
        check(guard.proactive_paused() is True, 'double threshold must pause today')

        # 关键：防线绝不再改写配置文件（旧实现把 enabled 永久写成 false）
        data = json.loads(config_path.read_text(encoding='utf-8'))
        check(data['proactive_enabled'] is True, 'usage guard must never rewrite proactive.json')

        # 跨天自动恢复
        guard._today = '2000-01-01'  # 伪装成昨天，下一次记账触发 _roll_day
        guard.record({'input': 10, 'output': 0, 'cacheRead': 0}, model='glm-5.3-flash')
        check(guard.proactive_paused() is False, 'pause must clear on day roll (auto-recover)')


def test_trigger_honors_pause_gate_and_config_switch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / 'proactive.json'
        cfg_path.write_text(json.dumps({'proactive_enabled': True}), encoding='utf-8')

        def make_trigger(gate) -> ProactiveTrigger:
            trigger = object.__new__(ProactiveTrigger)  # 不碰网关/密钥
            trigger._config = ProactiveConfig(path=cfg_path)
            trigger._pause_gate = gate
            return trigger

        check(make_trigger(lambda: False)._enabled() is True, 'gate open + config on -> enabled')
        check(make_trigger(lambda: True)._enabled() is False, 'guard pause -> disabled for today')
        check(make_trigger(lambda: (_ for _ in ()).throw(RuntimeError('boom')))._enabled() is True,
              'gate failure must fail open, never weld the ring shut')

        cfg_path.write_text(json.dumps({'proactive_enabled': False}), encoding='utf-8')
        check(make_trigger(lambda: False)._enabled() is False, 'master switch off still wins')


if __name__ == '__main__':
    test_guard_pause_is_ephemeral_and_never_touches_config()
    test_trigger_honors_pause_gate_and_config_switch()
    print('ALL PASS test_usage_guard_pause')
