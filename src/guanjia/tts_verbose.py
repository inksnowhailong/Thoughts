"""让服务端 verbose 打印分阶段耗时；这里只触发一次并报总耗时，阶段耗时看 tmp/tts_server.log。"""
import time
from guanjia.tts_client import TTSClient
c = TTSClient()
t0 = time.perf_counter(); pcm, sr = c.synth("现在几点了，帮我看一下明天的天气怎么样。", verbose=True); dt = time.perf_counter() - t0
print(f"audio {len(pcm)/sr:.2f}s synth {dt:.2f}s rtf {dt/(len(pcm)/sr):.2f}")
