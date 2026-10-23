"""智谱网关保险丝：夹在 OpenClaw 和智谱官方接口之间的本地转发（只转发，不存密钥，密钥由 OpenClaw 的 SecretRef 随请求头带来）。

为什么要有它：OpenClaw 对 429（限流/过载）、5xx、网络不通 这三类错误会先在原模型上重试（限流最多 10 次约 130 秒，
网络不通约 64 秒），之后才切备用，语音场景等不起。保险丝在"还没往回写任何字节"之前就判断上游是否可用，
不可用就立刻回一个带 Retry-After:120 的 429；OpenClaw 对「要求等待超过 60 秒」的限流会直接切备用
（官方文档 concepts/retry.md），不再原地重试。

规则：
  上游 200 且在 FIRSTBYTE 秒内来了第一块数据 → 原样流式转发（只多一次本机转发的开销）
  401/403/400/404 等其它 4xx                 → 原样透传（认证类 OpenClaw 本来就立刻切备用；400 是请求本身的问题，不该换模型重试）
  429 且是欠费/余额不足（1113）              → 原样透传（OpenClaw 按"账单类"立刻切备用并停用该凭据 10 分钟）
  其它 429（1302 限流 / 1305 过载）、5xx、连不上、首字超时 → 回 429 + Retry-After: 120（立刻切备用）
  任一"不可用"事件后 BREAKER 秒内（熔断窗口）直接回同样的 429，不再碰上游，网络挂了时每轮不多花连接超时。
日志：只记 时间/状态/耗时/原因，不含密钥与对话内容。
环境变量：GATE_PORT(18793) GATE_UPSTREAM(https://open.bigmodel.cn) GATE_FIRSTBYTE(25) GATE_BREAKER(20) GATE_LOG
"""
import json
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

PORT = int(os.environ.get("GATE_PORT", "18793"))
UPSTREAM = os.environ.get("GATE_UPSTREAM", "https://open.bigmodel.cn").rstrip("/")

# 思考档位默认值：环境变量优先，其次 data/config.json，最后 low
_DEFAULT_EFFORT = os.environ.get("GATE_REASONING_EFFORT")
if not _DEFAULT_EFFORT:
    try:
        _cfg = json.loads(open(os.environ.get("GUANJIA_ROOT", r"F:\coding\guanjia-v2-development")
                               + r"\data\config.json", encoding="utf-8").read())
        _DEFAULT_EFFORT = _cfg.get("models", {}).get("brain", {}).get("reasoning_effort") or "low"
    except Exception:
        _DEFAULT_EFFORT = "low"


def inject_defaults(raw: bytes) -> bytes:
    """请求体缺省参数注入：thinking.enabled（flash 强制项）+ reasoning_effort 默认档。

    只补缺失字段——请求体里已显式带的参数一律不动（干活的 high 在直调模块显式设置）。
    解析失败原样转发（fail-open，绝不因注入逻辑断链）。
    """
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            return raw
        changed = False
        thinking = body.get("thinking")
        if not isinstance(thinking, dict):
            body["thinking"] = {"type": "enabled"}
            changed = True
        elif thinking.get("type") is None:
            thinking["type"] = "enabled"
            changed = True
        if body.get("reasoning_effort") is None:
            body["reasoning_effort"] = _DEFAULT_EFFORT
            changed = True
        if changed:
            return json.dumps(body, ensure_ascii=False).encode("utf-8")
        return raw
    except Exception:
        return raw
FIRSTBYTE = float(os.environ.get("GATE_FIRSTBYTE", "25"))
BREAKER = float(os.environ.get("GATE_BREAKER", "20"))
LOG = pathlib.Path(os.environ.get("GATE_LOG", r"F:\coding\guanjia-v2-development\tmp\zhipu_gate.log"))
client = httpx.Client(timeout=httpx.Timeout(connect=3.0, read=FIRSTBYTE, write=10.0, pool=5.0), trust_env=False)
_open_until = 0.0
_lock = threading.Lock()


def log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def unavailable_body(reason: str) -> bytes:
    msg = f"rate limit: 智谱上游暂时不可用（{reason}），请稍后再试 / upstream temporarily unavailable"
    return json.dumps({"error": {"code": "gate_unavailable", "message": msg}}, ensure_ascii=False).encode("utf-8")


def is_billing(status: int, body: bytes) -> bool:
    if status != 429:
        return False
    t = body.decode("utf-8", "replace")
    return '"1113"' in t or "欠费" in t or "余额" in t


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _reply(self, status: int, body: bytes, extra: dict | None = None, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, reason: str, t0: float) -> None:
        global _open_until
        with _lock:
            _open_until = time.time() + BREAKER
        log(f"UNAVAILABLE {reason} {int((time.time() - t0) * 1000)}ms -> 429+Retry-After")
        self._reply(429, unavailable_body(reason), {"Retry-After": "120"})

    def do_POST(self):
        t0 = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        # 参数默认值注入（发起人 2026-10-23 需求：主模块 low、干活显式覆盖）：
        # glm-5.3-flash 强制开启思考且 reasoning_effort 默认 max——max 深思考
        # 计入输出 token 且拖慢首响；缺省注入 low 作为全链兜底，
        # 直调模块（评审/身份/生成/预热）在各自请求体里显式覆盖本默认。
        raw = inject_defaults(raw)
        if time.time() < _open_until:
            log("BREAKER-OPEN -> 429")
            self._reply(429, unavailable_body("熔断中"), {"Retry-After": "120"})
            return
        hdr = {k: v for k, v in self.headers.items() if k.lower() in ("authorization", "content-type", "accept")}
        started = False
        try:
            with client.stream("POST", UPSTREAM + self.path, content=raw, headers=hdr) as r:
                if r.status_code != 200:
                    body = r.read()
                    if is_billing(r.status_code, body) or (400 <= r.status_code < 500 and r.status_code != 429):
                        log(f"PASS {r.status_code} {int((time.time() - t0) * 1000)}ms")
                        self._reply(r.status_code, body, ctype=r.headers.get("content-type", "application/json"))
                        return
                    self._fail(f"HTTP {r.status_code}", t0)
                    return
                it = r.iter_raw()
                try:
                    first = next(it)  # 等第一块数据；超时会抛 ReadTimeout
                except StopIteration:
                    self._fail("empty stream", t0)
                    return
                self.send_response(200)
                self.send_header("Content-Type", r.headers.get("content-type", "text/event-stream"))
                self.end_headers()
                started = True
                self.wfile.write(first)
                self.wfile.flush()
                log(f"OK first-byte {int((time.time() - t0) * 1000)}ms")
                for chunk in it:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            self._fail(f"connect {type(e).__name__}", t0)
        except httpx.ReadTimeout:
            if started:
                log("stream read timeout mid-way")
            else:
                self._fail(f"first-byte>{int(FIRSTBYTE)}s", t0)
        except httpx.HTTPError as e:
            if started:
                log(f"stream broke mid-way {type(e).__name__}")
            else:
                self._fail(f"http {type(e).__name__}", t0)
        except (BrokenPipeError, ConnectionResetError):
            log("client closed")


if __name__ == "__main__":
    log(f"start port={PORT} upstream={UPSTREAM} firstbyte={FIRSTBYTE}s breaker={BREAKER}s")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


