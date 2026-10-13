"""假 TTS 进程，给 scripts/wsl-tts-launcher.ps1 的测试用：前 fail_n 次往 stderr 写一句话并以 1 退出；之后监听端口 hold 秒再退出。

用法：python tests\fake_tts_child.py <计数文件> <端口> <fail_n> [hold秒，默认 3] [退出码，默认 0] [模式]
  退出码：监听结束后的退出码（1 = 模拟崩溃 / OOM，75 = 模拟为游戏腾显存主动退出）
  模式 closeport：监听 3 秒后关掉端口但进程继续活着（模拟服务卡死 / 端口不通）
"""
import socket
import sys
import time
from pathlib import Path

counter, port, fail_n = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
hold = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0
exit_code = int(sys.argv[5]) if len(sys.argv) > 5 else 0
mode = sys.argv[6] if len(sys.argv) > 6 else ""
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
if n < fail_n:
    sys.stderr.write(f"wsl: fake failure {n + 1}\n")
    sys.exit(1)
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(5)
if mode == "closeport":
    time.sleep(3.0)
    srv.close()
time.sleep(hold)
sys.exit(exit_code)
