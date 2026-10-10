"""P2 打断检测与提示音用例（合成的电平序列，不碰音频设备）。

电平取自 10-01 日志：只有回声时原始峰值 0.00015~0.002（起播瞬间一个 0.013 的尖峰），主人插话时 0.03~0.1。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p2_barge_cues.py
"""
import numpy as np

from guanjia.audio_cues import EARCON_AMP, earcon
from guanjia.barge import BargeDetector


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def run(det: BargeDetector, frames: list[tuple[float, bool]]) -> int | None:
    """逐帧喂 (rms, 是否在出声)；返回第一次触发的帧序号，没触发返回 None。"""
    for i, (rms, ref) in enumerate(frames):
        if det.step(rms, ref):
            return i
    return None


ECHO = 0.0005

# 1. 只有自己的回声（开头 100ms 起播尖峰）不触发，触发线也不被尖峰抬太高
det = BargeDetector()
check(run(det, [(0.01, True)] * 10 + [(ECHO, True)] * 290) is None, "echo only (with start spike) never triggers")
check(det.line < 0.006, f"start spike does not inflate the line (line={det.line:.4f})")

# 2. 主人插话，中间夹着掉线帧（旧逻辑 VAD 一掉就清零）：250ms 内触发
det = BargeDetector()
speech = [(0.001 if k % 5 == 4 else 0.02, True) for k in range(40)]
hit = run(det, [(ECHO, True)] * 50 + speech)
check(hit is not None and 50 <= hit <= 75, f"barge-in with dropouts triggers within 250ms (frame {hit})")

# 3. 60ms 咳嗽不触发
det = BargeDetector()
check(run(det, [(ECHO, True)] * 50 + [(0.05, True)] * 6 + [(ECHO, True)] * 100) is None, "60ms cough does not trigger")

# 4. 没在出声时什么都不触发
det = BargeDetector()
check(run(det, [(0.05, False)] * 200) is None, "never triggers while not playing")

# 5. 保护期后底噪冻结，不跟着主人的声音涨
det = BargeDetector()
run(det, [(ECHO, True)] * 40)
f0 = det.floor
run(det, [(0.003, True)] * 30)
check(det.floor == f0, "floor frozen after guard window")

# 6. 参考信号停后 300ms 内仍算在播，之后复位
det = BargeDetector()
run(det, [(ECHO, True)] * 40)
run(det, [(ECHO, False)] * 30)
check(det.playing and det.play_ms > 0, "tail: still playing within 300ms after reference stops")
det.step(ECHO, False)
check(not det.playing and det.play_ms == 0, "tail: reset after 300ms")

# 7. 触发后累计清零
det = BargeDetector()
hit = run(det, [(ECHO, True)] * 50 + [(0.02, True)] * 40)
check(hit is not None and det.hot_ms == 0, "hot counter cleared after trigger")

# 8. 提示音
for sr in (16000, 48000):
    e = earcon(sr)
    check(e.dtype == np.float32 and len(e) == sr * 220 // 1000, f"{sr}Hz earcon is 0.22s float32")
    peak = float(np.max(np.abs(e)))
    check(0.9 * EARCON_AMP <= peak <= EARCON_AMP + 1e-6, f"{sr}Hz earcon peak {peak:.3f} within amplitude")
    check(abs(float(e[0])) < 1e-6 and abs(float(e[-1])) < 1e-6, f"{sr}Hz earcon fades in/out to zero")

print("ALL PASS test_p2_barge_cues")
