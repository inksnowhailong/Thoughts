"""假语音进程：按计数文件依次以 3、1、0 退出，用来测 voice-supervisor.ps1 的拉起策略；带 hold 参数时睡 6 秒后以 0 退出；
带 crash 参数时第一次往 stderr 写一段假堆栈后以 1 退出，第二次以 0 退出（测看门狗是否把 stderr 存档、尾巴写进日志）。

用法：python tests\\fake_voice_child.py <计数文件> [hold|crash]
"""
import sys
import time
from pathlib import Path

counter = Path(sys.argv[1])
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
if len(sys.argv) > 2 and sys.argv[2] == "hold":
    time.sleep(6)
    sys.exit(0)
if len(sys.argv) > 2 and sys.argv[2] == "crash":
    if n == 0:
        sys.stderr.write("Traceback (most recent call last):\n  File \"fake.py\", line 1, in <module>\nRuntimeError: boom-fake-stderr\n")
        sys.stderr.flush()
        sys.exit(1)
    sys.exit(0)
codes = [3, 1, 0]
sys.exit(codes[min(n, len(codes) - 1)])