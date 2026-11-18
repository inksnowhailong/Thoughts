"""大脑客户端：通过 OpenAI 兼容接口流式调用当前 agent-runtime。

默认地址是本机 18800；GUANJIA_BRAIN_URL 可覆盖。默认令牌来自
agent-runtime/config/runtime.json；GUANJIA_GATEWAY_TOKEN 可显式覆盖。
`x-openclaw-*` 请求头名是运行时保留的兼容协议字段，不表示 OpenClaw 网关正在运行。

语音打断只停止播放，必须继续读取到 [DONE]，避免把客户端断开当成本轮取消；
user 会话参数也为旧调用方保留兼容。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx

from guanjia.prompts import render_voice_tag

# 默认使用当前 agent-runtime。
DEFAULT_URL = "http://127.0.0.1:18800/v1/chat/completions"
RUNTIME_URL = os.environ.get("GUANJIA_BRAIN_URL", DEFAULT_URL)
MAIN_SESSION_KEY = "agent:main:main"  # 语音主线的稳定会话键
# 运行时接受的兼容模型覆盖头；空串时使用服务端默认模型。
VOICE_MODEL = os.environ.get("GUANJIA_VOICE_MODEL", "")
# 流式读超时：要远长于 L3 审批（120 秒）和慢工具。读超时会断开连接 = 取消本轮，宁可多等
STREAM_READ_TIMEOUT_SEC = 900.0


def voice_tag(now: datetime | None = None, speaker: str = "") -> str:
    """语音轮标记，例如「[语音 2026-10-10 18:05 周六]」，声纹认出说话人时末尾带上，如「[语音 2026-10-10 18:05 周六 主人]」；AGENTS.md 的语音规则和分级插件都认这个前缀。
    格式来自可在面板编辑的模板（prompts.render_voice_tag），默认与原来逐字节一致，模板不合格时回退内置格式。"""
    return render_voice_tag(now, speaker)


def tag_voice_text(text: str, now: datetime | None = None, speaker: str = "") -> str:
    """给识别出的一句话加上语音轮标记；speaker 是声纹认出的说话人（"主人" / "未知" / 库里的名字），空则不带。"""
    return f"{voice_tag(now, speaker)} {text}"


def load_gateway_token() -> str:
    env = os.environ.get("GUANJIA_GATEWAY_TOKEN")
    if env:
        return env
    # 默认本机 agent-runtime 使用 runtime.json 中的共享 token。
    if ":18800" in RUNTIME_URL:
        runtime_json = Path(__file__).resolve().parents[2] / "agent-runtime" / "config" / "runtime.json"
        try:
            data = json.loads(runtime_json.read_text(encoding="utf-8"))
            token = data.get("token")
            if isinstance(token, str) and token.strip():
                return token.strip()
        except (OSError, ValueError):
            pass
    cfg = Path.home() / ".openclaw" / "openclaw.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    return data["gateway"]["auth"]["token"]


class Brain:
    """当前大脑运行时的最小客户端。"""

    def __init__(self, url: str = RUNTIME_URL, token: str | None = None, timeout: float = STREAM_READ_TIMEOUT_SEC,
                 model: str | None = None, user: str | None = None, session_key: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.url = url
        self.model = VOICE_MODEL if model is None else model  # 兼容模型覆盖头；空串不加
        self.user = user  # 旧方式：按 user 派生会话（只在没有 session_key 时发送）
        self.session_key = session_key  # 新方式：显式会话键，例如 agent:main:main
        self.token = token or load_gateway_token()
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False,  # 本机服务，不走系统代理
                                    transport=transport)
        self._resp: httpx.Response | None = None  # 进行中的流式响应（仅供旧代码 abort 用，语音侧不调用）
        self.last_run_id: str | None = None  # 最近一轮的 runId，供调试事件关联耗时
        self.last_meta: dict = {}  # 最近一轮流里带的元信息：chunks / model / finish_reason / usage（有才有），统一事件日志用

    def _headers(self, model_override: str | None = None, traceparent: str | None = None,
                 trace_started_at: str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if traceparent:
            h["traceparent"] = traceparent
        if trace_started_at:
            h["x-observability-started-at"] = trace_started_at
        effective = model_override or self.model
        if effective:
            h["x-openclaw-model"] = effective
        if self.session_key:
            h["x-openclaw-session-key"] = self.session_key
            # 多 agent 路由：从会话键 agent:<id>:… 取目标 agent。
            parts = self.session_key.split(":")
            if len(parts) >= 2 and parts[0] == "agent":
                h["x-openclaw-agent-id"] = parts[1]
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

    def stream(self, text: str, system: str | None = None, *, model: str | None = None,
               traceparent: str | None = None, trace_started_at: str | None = None) -> Iterator[str]:
        """流式：逐段 yield 回复文本；流里出现 error 对象时抛 RuntimeError（服务端已结束这一轮）。

        model 参数：单次请求的兼容模型覆盖头——
        游戏模式中传 'glm-5.3'（完整版，比 flash 知识深度高一个量级）用于攻略分析。
        """
        self.last_run_id = None
        meta = self.last_meta = {}  # 每轮换一个新字典，上一轮留给事件日志读的那份不会被改
        with self._client.stream("POST", self.url, headers=self._headers(model, traceparent, trace_started_at),
                                 json=self._body(text, system, stream=True)) as r:
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
