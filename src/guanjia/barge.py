"""打断检测（与音频设备无关，可单测）：我们在出声时，麦克风原始电平持续明显高于"自己的回声"就判定主人插话。

为什么不再要求 VAD 判人声（P2 改动的根因）：这台 EDIFIER 音箱条在播放时用内置 DSP 处理麦克风输入，
主人插话时原始峰值有 0.03~0.1，但 Silero VAD 在被处理过的信号上几乎判不出人声（10-01 06:07 日志 barge-mon
采样全是 voice=False），旧逻辑"VAD 人声 且 能量过线、VAD 一掉就清零"于是永远攒不够 180ms。另外旧底噪在
"没过线"的帧上持续学习，主人一开口底噪就跟着涨（06:07:43 floor 0.0007→0.0182），线被抬走。

现在：
  每次开始出声后的保护期（300ms）只学回声底噪，前 100ms 不计（避开起播尖峰），保护期后冻结；
  触发线 = max(绝对下限, 底噪 × 倍数)；平滑能量过线就累加，掉线按 decay 回落（漏桶），累计到 180ms 即打断。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BargeConfig:
    frame_ms: int = 10          # 每帧时长
    guard_ms: int = 300         # 出声后的保护期：只学回声底噪，不触发
    learn_after_ms: int = 100   # 保护期里从这之后才计入底噪（起播瞬间有尖峰）
    tail_ms: int = 300          # 参考信号停了以后仍按"在播"算的尾巴（输出延迟 + 混响）
    rms_min: float = 0.004      # 触发线绝对下限（原始输入 RMS，增益前）
    floor_ratio: float = 3.0    # 触发线 = 回声底噪 × 此倍数
    trigger_ms: int = 180       # 累计过线时长达到此值即打断
    decay_ms: int = 5           # 没过线时每帧回落的累计毫秒（一两帧掉线不清零）
    smooth: float = 0.3         # 能量 EMA 系数（约 30ms 时间常数）


class BargeDetector:
    """每 10ms 喂一帧 step(rms, ref_active)，返回 True 表示该打断了。只在前端线程使用。"""

    def __init__(self, cfg: BargeConfig = BargeConfig()) -> None:
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        """全部状态归零（启动、游戏态恢复、静音恢复时调用）。"""
        self.since_ref_ms = 10**9  # 距最近一帧"我们在出声"的毫秒数
        self.play_ms = 0           # 本次连续出声已持续的毫秒数
        self.floor = 0.0           # 回声底噪（保护期内的平均能量）
        self._floor_n = 0
        self.energy = 0.0          # 平滑后的原始输入 RMS
        self.hot_ms = 0            # 过线累计（漏桶）

    @property
    def playing(self) -> bool:
        return self.since_ref_ms <= self.cfg.tail_ms

    @property
    def line(self) -> float:
        return max(self.cfg.rms_min, self.floor * self.cfg.floor_ratio)

    def step(self, rms: float, ref_active: bool) -> bool:
        c = self.cfg
        self.since_ref_ms = 0 if ref_active else self.since_ref_ms + c.frame_ms
        self.energy += c.smooth * (rms - self.energy)
        if not self.playing:
            self.play_ms, self.hot_ms, self.floor, self._floor_n = 0, 0, 0.0, 0
            return False
        self.play_ms += c.frame_ms
        if self.play_ms <= c.guard_ms:
            if self.play_ms > c.learn_after_ms:
                self._floor_n += 1
                self.floor += (self.energy - self.floor) / self._floor_n
            return False
        if self.energy > self.line:
            self.hot_ms += c.frame_ms
        else:
            self.hot_ms = max(0, self.hot_ms - c.decay_ms)
        if self.hot_ms >= c.trigger_ms:
            self.hot_ms = 0
            return True
        return False
