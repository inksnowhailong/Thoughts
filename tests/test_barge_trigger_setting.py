"""打断累计时长设置（2026-11-06 用户反馈）回归测试。

反馈：每次说话都会打断管家说话。原 BARGE_MS=180ms——一个音节就掐掉整句。
修复：改为 Settings.barge_trigger_ms（默认 450ms），面板热调；
rms_min/触发线逻辑不变。
"""
from __future__ import annotations

from dataclasses import replace
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.settings import Settings, from_dict
from guanjia.barge import BargeConfig, BargeDetector


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_default_raised_and_configurable() -> None:
    base = Settings()
    check(base.barge_trigger_ms == 450, f'default must be 450ms, got {base.barge_trigger_ms}')
    tuned = from_dict({'barge_trigger_ms': 700}, base)
    check(tuned.barge_trigger_ms == 700, 'barge_trigger_ms must be settable')
    for bad, why in (({'barge_trigger_ms': 50}, 'below range'), ({'barge_trigger_ms': 9999}, 'above range'),
                     ({'barge_trigger_ms': 'abc'}, 'non-numeric')):
        try:
            from_dict(bad, base)
            raise AssertionError(f'{why} must be rejected')
        except ValueError:
            pass


def test_detector_hot_swap_uses_setting() -> None:
    detector = BargeDetector()
    check(detector.cfg.trigger_ms == 180, 'Bare BargeConfig keeps its own default (existing timing tests rely on it)')
    detector.cfg = replace(detector.cfg, rms_min=0.006, trigger_ms=450)
    check(detector.cfg.trigger_ms == 450 and detector.cfg.rms_min == 0.006, 'hot apply must swap both knobs')


def test_sustained_short_sound_does_not_trip() -> None:
    # 出声期先播 300ms 回声（保护期里检测器学底噪），随后 500ms 的插话
    # 在 450ms 新阈值下不应打断——过线累计只有 200ms（旧 180ms 阈值下会掐）
    detector = BargeDetector(BargeConfig(trigger_ms=450))
    for _ in range(30):                       # 300ms 保护期：只有回声
        check(detector.step(0.002, True) is False, 'guard period never trips')
    for _ in range(20):                       # 200ms 插话：过线但不够久
        check(detector.step(0.05, True) is False, 'a 200ms interjection must not interrupt under 450ms threshold')
    check(detector.hot_ms == 200, f'interjection is above the line, not yet sustained: hot={detector.hot_ms}ms')
    # 再开口 250ms：累计到 450ms 触发一次打断（真实插话仍然好使）
    hit_frames = 0
    for _ in range(25):
        hit_frames += 1
        if detector.step(0.05, True):
            break
    check(hit_frames <= 25, 'a real sustained interjection still trips the interrupt')


if __name__ == '__main__':
    test_default_raised_and_configurable()
    test_detector_hot_swap_uses_setting()
    test_sustained_short_sound_does_not_trip()
    print('ALL PASS test_barge_trigger_setting')
