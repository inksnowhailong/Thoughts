"""大脑客户端：把识别出的文字交给 OpenClaw 网关（OpenAI 兼容的 /v1/chat/completions），流式收回回复文本。

- token 从 ~/.openclaw/openclaw.json 读（gateway.auth.token），也可用环境变量 GUANJIA_GATEWAY_TOKEN 覆盖；
- 语音（P2 起）：带 x-openclaw-session-key: agent:main:main 进主线会话，不带 user、不带 system、默认不换模型
  （走网关默认 DeepSeek-V4-Flash）；每句话前加「[语音 时间]」标记，AGENTS.md 据此只在语音轮简短作答。
  标记放在用户消息里而不是 system：system 每轮变化会打掉网关的提示缓存，首字更慢；
- stream(text) 是同步生成器，逐段 yield 回复文本。语音侧不得提前关闭它：网关把客户端断开当作取消本轮
  （连同这一轮里等着的审批），所以打断只停播放，流要读到 [DONE]；读超时同样会断开，故设得很长；
- user 会话方式（backoffice 用）保留兼容。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx

from guanjia.prompts import render_voice_tag

DEFAULT_URL = "http://127.0.0.1:18789/v1/chat/completions"
MAIN_SESSION_KEY = "agent:main:main"  # 主线会话：主人的语音 + 微信共用
# 语音入口指定的网关模型（x-openclaw-model 头）；空串 = 不加头，走网关默认模型（DeepSeek-V4-Flash）
VOICE_MODEL = os.environ.get("GUANJIA_VOICE_MODEL", "")
# 流式读超时：要远长于 L3 审批（120 秒）和慢工具。读超时会断开连接 = 取消本轮，宁可多等
STREAM_READ_TIMEOUT_SEC = 900.0


def voice_tag(now: datetime | None = None) -> str:
    """语音轮标记，例如「[语音 2026-10-10 18:05 周六]」；AGENTS.md 的语音规则和分级插件都认这个前缀。
    格式来自可在面板编辑的模板（prompts.render_voice_tag），默认与原来逐字节一致，模板不合格时回退内置格式。"""
    return render_voice_tag(now)


def tag_voice_text(text: str, now: datetime | None = None) -> str:
    """给识别出的一句话加上语音轮标记。"""
    return f"{voice_tag(now)} {text}"


def load_gateway_token() -> str:
    env = os.environ.get("GUANJIA_GATEWAY_TOKEN")
    if env:
        return env
    cfg = Path.home() / ".openclaw" / "openclaw.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return data["gateway"]["auth"]["token"]


class Brain:
    """OpenClaw 网关的最小客户端。"""

    def __init__(self, url: str = DEFAULT_URL, token: str | None = None, timeout: float = STREAM_READ_TIMEOUT_SEC,
                 model: str | None = None, user: str | None = None, session_key: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.url = url
        self.model = VOICE_MODEL if model is None else model  # x-openclaw-model 头；空串不加
        self.user = user  # 旧方式：网关按 user 派生会话（只在没有 session_key 时发送）
        self.session_key = session_key  # 新方式：显式会话键，例如 agent:main:main
        self.token = token or load_gateway_token()
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False,  # 本机网关，不走系统代理
                                    transport=transport)
        self._resp: httpx.Response | None = None  # 进行中的流式响应（仅供旧代码 abort 用，语音侧不调用）
        self.last_run_id: str | None = None  # 最近一轮的网关 runId（chatcmpl_…），调试面板据此查网关内部耗时
        self.last_meta: dict = {}  # 最近一轮流里带的元信息：chunks / model / finish_reason / usage（有才有），统一事件日志用

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if self.model:
            h["x-openclaw-model"] = self.model
        if self.session_key:
            h["x-openclaw-session-key"] = self.session_key
        return h

    def _body(self, text: str, system: str | None, stream: bool) -> dict:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
        body: dict = {"model": "openclaw", "messages": msgs}
        if stream:
            body["stream"] = True
        if self.user and not self.session_key:
            body["user"] = self.user
        return body

    def ask(self, text: str, system: str | None = None) -> str:
        """非流式：整句回复（后台任务用）。"""
        r = self._client.post(self.url, headers=self._headers(), json=self._body(text, system, stream=False))
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def stream(self, text: str, system: str | None = None) -> Iterator[str]:
        """流式：逐段 yield 回复文本；流里出现 error 对象时抛 RuntimeError（网关已经结束这一轮）。"""
        self.last_run_id = None
        meta = self.last_meta = {}  # 每轮换一个新字典，上一轮留给事件日志读的那份不会被改
        with self._client.stream("POST", self.url, headers=self._headers(), json=self._body(text, system, stream=True)) as r:
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
                    if self.last_run_id is None and isinstance(chunk.get("id"), str):
                        self.last_run_id = chunk["id"]
                    err = chunk.get("error")
                    if isinstance(err, dict):
                        raise RuntimeError(f"网关报错：{err.get('message') or err.get('type') or err}")
                    # 纯内存字典赋值，不影响首字；流里有才记（不加 stream_options，避免改变网关行为）
                    meta["chunks"] = meta.get("chunks", 0) + 1
                    if chunk.get("model"):
                        meta["model"] = chunk["model"]
                    choice = (chunk.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        meta["finish_reason"] = choice["finish_reason"]
                    if isinstance(chunk.get("usage"), dict):
                        meta["usage"] = chunk["usage"]
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        yield delta
            finally:
                self._resp = None

    def abort(self) -> None:
        """旧代码用的强制关闭；语音守护进程自 P2 起不调用（关闭连接会让网关取消本轮）。"""
        r = self._resp
        if r is not None:
            r.close()

    def close(self) -> None:
        self._client.close()
