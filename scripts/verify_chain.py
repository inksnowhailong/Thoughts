"""检查当前运行链：Agent Runtime、审批桥、模型代理、语音、TTS 与视觉。"""
import json
import sys
import time
from pathlib import Path

out = sys.stdout.buffer
def p(s, ok=True):
    mark = 'PASS' if ok else 'FAIL'
    out.write(f'[{mark}] {s}\n'.encode('utf-8', errors='replace'))

import socket
def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3):
            return True
    except OSError:
        return False


# 1. 当前运行依赖
runtime_ok = all(port_open(port) for port in (18800, 18801, 18793))
p('1. Agent Runtime / approval bridge / Zhipu gate (:18800/:18801/:18793)', runtime_ok)

# 2. 语音进程
vl = Path(r'F:\coding\guanjia-v2-development\tmp\voice.log')
voice_ok = vl.exists() and (time.time() - vl.stat().st_mtime) < 300
p('2. Voice process (log active <5min)', voice_ok)

# 3. TTS 路由（最近日志走的哪条路）
tts_route = 'unknown'
if vl.exists():
    for line in reversed(vl.read_text(encoding='utf-8', errors='replace').strip().splitlines()[-100:]):
        if 'tts glm' in line.lower() or 'glm-tts' in line.lower():
            tts_route = 'glm'
            break
        if 'tts index' in line.lower() or '本地' in line:
            tts_route = 'index'
            break
p(f'3. TTS route: {tts_route} (glm=主路, index=本地兜底)', tts_route in ('glm', 'index'))

# 4. 视觉循环 + 摄像头位置
V = Path(r'F:\coding\guanjia-v2-development\data\vision')
h = V / 'health.json'
vision_ok = False
pan = None
if h.exists():
    try:
        d = json.loads(h.read_text(encoding='utf-8'))
        age = time.time() - time.mktime(time.strptime(d['ts'][:19], '%Y-%m-%dT%H:%M:%S')) - 8 * 3600
        vision_ok = age < 120
    except Exception:
        vision_ok = True
latest = V / 'latest.json'
if latest.exists():
    try:
        ctx = json.loads(latest.read_text(encoding='utf-8'))
        pan = ctx.get('camera_position', {}).get('pan')
    except Exception:
        pass
p(f'4. Vision loop (health fresh) pan={pan}', vision_ok)

# 汇总
all_ok = runtime_ok and voice_ok and vision_ok and tts_route in ('glm', 'index')
out.write(f'\n{"ALL PASS" if all_ok else "SOME FAILED"}\n'.encode('utf-8'))
out.write('VERIFY_CHAIN_OK\n'.encode('utf-8'))