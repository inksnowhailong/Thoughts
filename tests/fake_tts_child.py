"""假 TTS 进程（测 scripts/wsl-tts-launcher.ps1 的重试）：前 fail_n 次往 stderr 写一句话后以 1 退出，之后监听端口 hold 秒再以 0 退出。

用法：python tests\\fake_tts_child.py <计数文件> <端口> <fail_n> [hold秒，默认 3]
"""
import socket
import sys
import time
from pathlib import Path

counter, port, fail_n = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
hold = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
if n < fail_n:
    sys.stderr.write(f"wsl: fake failure {n + 1}\n")
    sys.exit(1)
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(5)
time.sleep(hold)
sys.exit(0)
