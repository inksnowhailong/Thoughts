"""合成一句的同时每 0.5s 采样一次 GPU 利用率，判断瓶颈在 GPU 还是 CPU。"""
import subprocess, threading, time
from guanjia.tts_client import TTSClient

samples = []
stop = threading.Event()
def sampler():
    while not stop.is_set():
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,clocks.sm,power.draw", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
        samples.append(out); time.sleep(0.5)
th = threading.Thread(target=sampler, daemon=True); th.start()
c = TTSClient()
t0 = time.perf_counter(); pcm, sr = c.synth("现在几点了，帮我看一下明天的天气怎么样。"); dt = time.perf_counter() - t0
stop.set(); th.join()
print(f"audio {len(pcm)/sr:.2f}s synth {dt:.2f}s rtf {dt/(len(pcm)/sr):.2f}")
for s in samples: print("  ", s)
