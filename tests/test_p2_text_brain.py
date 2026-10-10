"""P2 文本层用例：speakable 过滤、语音轮标记、网关客户端进主线会话（假传输，不发真实请求）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p2_text_brain.py
"""
import json
from datetime import datetime

import httpx

from guanjia.brain import MAIN_SESSION_KEY, STREAM_READ_TIMEOUT_SEC, Brain, tag_voice_text, voice_tag
from guanjia.textseg import speakable


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode("utf-8")


def delta(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


# ---------- speakable ----------
check(speakable("**好的**，马上办。") == "好的，马上办。", "strip markdown bold")
check(speakable("## 今日安排") == "今日安排", "strip heading marks")
check(speakable("- 第一条") == "第一条", "strip bullet")
check(speakable("1. 打开记事本") == "打开记事本", "strip numbered bullet")
check(speakable("温度 1.5 度") == "温度 1.5 度", "decimal is not a bullet")
check("http" not in speakable("详见 https://example.com/a?b=1 。"), "strip url")
check(speakable("NO_REPLY") == "" and speakable(" no_reply ") == "", "silent marker not spoken")
check(speakable("😀👍") == "", "emoji only not spoken")
check(speakable("……") == "" and speakable("。") == "", "punctuation only not spoken")
check(speakable("好的👌") == "好的", "strip emoji keep words")

# ---------- 语音轮标记 ----------
t = datetime(2026, 10, 10, 18, 5)
check(voice_tag(t) == "[语音 2026-10-10 18:05 周六]", "voice tag format")
check(tag_voice_text("打开记事本", t) == "[语音 2026-10-10 18:05 周六] 打开记事本", "voice tag prefix")

# ---------- 网关客户端：主线会话 ----------
seen: dict = {}


def handler(request: httpx.Request) -> httpx.Response:
    seen["url"] = str(request.url)
    seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
    seen["body"] = json.loads(request.content)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([delta("你好，"), delta("我在。")]))


b = Brain(token="t", session_key=MAIN_SESSION_KEY, model="", transport=httpx.MockTransport(handler))
out = list(b.stream(tag_voice_text("你好", t)))
check(out == ["你好，", "我在。"], "stream yields deltas")
check(seen["url"] == "http://127.0.0.1:18789/v1/chat/completions", "local gateway url")
check(seen["headers"].get("x-openclaw-session-key") == "agent:main:main", "main session header")
check("x-openclaw-model" not in seen["headers"], "no model override (gateway default DeepSeek-V4-Flash)")
check(seen["headers"].get("authorization") == "Bearer t", "gateway token header")
check("user" not in seen["body"], "no user field when session key is set")
check(seen["body"]["messages"] == [{"role": "user", "content": "[语音 2026-10-10 18:05 周六] 你好"}], "single user message, no system")
check(seen["body"]["stream"] is True and seen["body"]["model"] == "openclaw", "streaming, agent target openclaw")
check(b._client.timeout.read == STREAM_READ_TIMEOUT_SEC == 900.0, "read timeout 900s (longer than 120s approvals)")


def err_handler(request: httpx.Request) -> httpx.Response:
    chunks = [delta("好"), {"error": {"message": "agent run failed", "type": "api_error"}}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(chunks))


b2 = Brain(token="t", session_key=MAIN_SESSION_KEY, model="", transport=httpx.MockTransport(err_handler))
got, raised = [], ""
try:
    for p in b2.stream("x"):
        got.append(p)
except RuntimeError as e:
    raised = str(e)
check(got == ["好"] and "agent run failed" in raised, "error object in stream raises RuntimeError")


def legacy(request: httpx.Request) -> httpx.Response:
    seen["legacy_headers"] = {k.lower(): v for k, v in request.headers.items()}
    seen["legacy_body"] = json.loads(request.content)
    return httpx.Response(200, json={"choices": [{"message": {"content": "办好了"}}]})


b3 = Brain(token="t", user="voice-backoffice-1", model="siliconflow/x", transport=httpx.MockTransport(legacy))
check(b3.ask("开记事本") == "办好了", "ask non-streaming")
check(seen["legacy_body"].get("user") == "voice-backoffice-1" and "stream" not in seen["legacy_body"], "legacy user session kept")
check(seen["legacy_headers"].get("x-openclaw-model") == "siliconflow/x"
      and "x-openclaw-session-key" not in seen["legacy_headers"], "legacy model header, no session header")

print("ALL PASS test_p2_text_brain")
