"""TTS 客户端：调本机的 IndexTTS 2 小服务（tts_server.py，跑在 index-tts 自己的虚拟环境里），文本进、float32 PCM 出。

为什么拆成服务：IndexTTS 的依赖（torch/CUDA）和守护进程的依赖互不兼容，分进程最省事；模型常驻在服务里，每句合成不重复加载。
"""

from __future__ import annotations

import io
import wave

import os

import httpx
import numpy as np

# 环境变量 GUANJIA_TTS_URL 可切换 TTS 服务地址（Windows 18790 / WSL2 18791）
DEFAULT_URL = os.environ.get("GUANJIA_TTS_URL", "http://127.0.0.1:18791/tts")  # 默认 WSL2 的 2.5；Windows 2.0 备用为 18790


class TTSClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 30.0) -> None:
        self.url = url
        self._client = httpx.Client(timeout=timeout, trust_env=False)  # 本机服务，不走系统代理
        self._resp: httpx.Response | None = None  # 进行中的合成请求，供 abort() 从别的线程关闭

    def synth(self, text: str, **gen_kwargs) -> tuple[np.ndarray, int]:
        """返回 (float32 单声道 PCM, 采样率)。失败抛异常，由调用方决定是否跳过这句。gen_kwargs 透传给 IndexTTS infer。"""
        with self._client.stream("POST", self.url, json={"text": text, **gen_kwargs}) as r:
            self._resp = r
            try:
                r.raise_for_status()
                data = r.read()
            finally:
                self._resp = None
        with wave.open(io.BytesIO(data), "rb") as w:
            sr, n_ch = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if n_ch > 1:
            pcm = pcm.reshape(-1, n_ch).mean(axis=1)
        return pcm, sr

    def abort(self) -> None:
        """从其他线程关闭当前合成请求（打断用），让阻塞中的 synth 立即抛异常返回。"""
        r = self._resp
        if r is not None:
            r.close()

    def control(self, action: str, timeout: float = 120.0) -> bool:
        """向服务发 /unload 或 /load（游戏态释放显存 / 恢复后预加载）；可选能力，失败只返回 False 不抛异常。"""
        try:
            return self._client.post(self.url.rsplit("/", 1)[0] + "/" + action, timeout=timeout).status_code == 200
        except httpx.HTTPError:
            return False

    def alive(self) -> bool:
        try:
            return self._client.get(self.url.rsplit("/", 1)[0] + "/health", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False


if __name__ == "__main__":
    import sys
    import time

    c = TTSClient()
    print("alive:", c.alive())
    t0 = time.perf_counter()
    pcm, sr = c.synth(" ".join(sys.argv[1:]) or "你好，我是管家。")
    print(f"{len(pcm) / sr:.2f}s audio @ {sr} Hz in {time.perf_counter() - t0:.2f}s")
    with wave.open("tmp/tts_test.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
