"""大脑客户端：把识别出的文字交给 OpenClaw 网关（OpenAI 兼容的 /v1/chat/completions），流式收回回复文本。

- token 从 ~/.openclaw/openclaw.json 读（gateway.auth.token），也可用环境变量 GUANJIA_GATEWAY_TOKEN 覆盖；
- `stream(text)` 是同步生成器，逐段 yield 回复文本；调用方随时 `close()` 生成器即视为打断，底层连接随之关闭；
- 会话由网关按 user 字段隔离；这里固定一个 user，保证语音入口是同一条会话。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx

DEFAULT_URL = "http://127.0.0.1:18789/v1/chat/completions"
# 语音入口指定的网关模型（x-openclaw-model 头，写法 siliconflow/<SiliconFlow 模型 id>）：
# Qwen3.8-27B 实测首 token 0.43~0.58s，口语质量优于 Qwen3.6 与 DeepSeek-V4-Flash。
# 环境变量 GUANJIA_VOICE_MODEL 可改；设为空串则不加头，走网关默认模型
VOICE_MODEL = os.environ.get("GUANJIA_VOICE_MODEL", "siliconflow/Qwen/Qwen3.8-27B")
VOICE_USER = "voice-desktop"


def load_gateway_token() -> str:
    env = os.environ.get("GUANJIA_GATEWAY_TOKEN")
    if env:
        return env
    cfg = Path.home() / ".openclaw" / "openclaw.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return data["gateway"]["auth"]["token"]


class Brain:
    """OpenClaw 网关的最小客户端。"""

    def __init__(self, url: str = DEFAULT_URL, token: str | None = None, timeout: float = 120.0,
                 model: str | None = None, user: str = VOICE_USER) -> None:
        self.url = url
        self.model = VOICE_MODEL if model is None else model  # x-openclaw-model 头；后台可指定别的模型
        self.user = user  # 网关会话隔离键
        self.token = token or load_gateway_token()
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False)  # 本机网关，不走系统代理
        self._resp: httpx.Response | None = None  # 进行中的流式响应，供 abort() 从别的线程关闭

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if self.model:
            h["x-openclaw-model"] = self.model
        return h

    def ask(self, text: str, system: str | None = None) -> str:
        """非流式：整句回复（后台任务用）。"""
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
        r = self._client.post(self.url, headers=self._headers(), json={
            "model": "openclaw", "user": self.user, "messages": msgs,
        })
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def stream(self, text: str, system: str | None = None) -> Iterator[str]:
        """流式：逐段 yield 回复文本。生成器被 close() 时中断请求（打断用）。system 为语音入口的附加约束。"""
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
        body = {"model": "openclaw", "user": self.user, "stream": True, "messages": msgs}
        with self._client.stream("POST", self.url, headers=self._headers(), json=body) as r:
            self._resp = r
            try:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield delta
            finally:
                self._resp = None

    def abort(self) -> None:
        """从其他线程关闭当前流式响应（打断用）：阻塞在 iter_lines 里的读会立即抛异常退出，服务端随连接断开停止生成。"""
        r = self._resp
        if r is not None:
            r.close()

    def close(self) -> None:
        self._client.close()


if __name__ == "__main__":
    import sys
    import time

    b = Brain()
    q = " ".join(sys.argv[1:]) or "用一句话说你是谁"
    t0 = time.perf_counter()
    first = None
    out = []
    for piece in b.stream(q):
        if first is None:
            first = time.perf_counter() - t0
        out.append(piece)
    print("".join(out))
    print(f"[first token {first:.2f}s, total {time.perf_counter() - t0:.2f}s]")
