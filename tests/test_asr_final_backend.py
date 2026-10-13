r"""二次识别的后端选择：FireRedASR2（GPU 服务）为主，SenseVoice 2024 回退；超时/出错/熔断/探活；游戏态释放与恢复。
不加载真模型、不连真服务：假服务端 + 假 SenseVoice + 假时钟。

运行：cd /d F:\coding\guanjia && .venv\Scripts\python.exe tests\test_asr_final_backend.py
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np

from guanjia.asr_final import (BREAKER_FAILS, PAD_SEC, SR, FinalASR, FinalASRConfig, FinalResult, resolve_backend)


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


# ---------- 1. 后端选择 ----------
check(resolve_backend({}) == "fireredasr2", "default backend = fireredasr2")
check(resolve_backend({"GUANJIA_FINAL_ASR_MODEL": "fireredasr2"}) == "fireredasr2", "env fireredasr2")
check(resolve_backend({"GUANJIA_FINAL_ASR_MODEL": "2024"}) == "sensevoice", "env 2024 -> SenseVoice only")
check(resolve_backend({"GUANJIA_FINAL_ASR_MODEL": "2025"}) == "sensevoice", "env 2025 -> SenseVoice only")
check(resolve_backend({"GUANJIA_FINAL_ASR_MODEL": "2031"}) == "fireredasr2", "typo -> default, no crash")


# ---------- 假件 ----------
class FakeRemote:
    def __init__(self, text="管家打开记事本", delay=0.0, fail=None, conf=0.97, min_conf=0.6):
        self.text, self.delay, self.fail, self.conf, self.min_conf = text, delay, fail, conf, min_conf
        self.calls, self.lens, self.controls, self.alive_val = 0, [], [], True
        self.free_mb = 6000
        self.warms, self.warmed = 0, threading.Event()

    def recognize(self, samples, timeout=None):
        self.calls += 1
        self.lens.append(len(samples))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return {"text": self.text, "conf": self.conf, "min_conf": self.min_conf, "ms": 55.0}

    def alive(self, timeout=0.3):
        return self.alive_val

    def stats(self, timeout=2.0):
        return {"loaded": True, "gpu_free_mb": self.free_mb}

    def warm(self, timeout=1.0):
        self.warms += 1
        self.warmed.set()
        return True

    def control(self, action, timeout=120.0):
        self.controls.append(action)
        return True


class FakeSV:
    def __init__(self, text="管家打开记事本。"):
        self.text, self.seen = text, 0

    def create_stream(self):
        sv = self

        class S:
            result = SimpleNamespace(text=sv.text)

            def accept_waveform(self, sr, samples):
                sv.seen += 1

        return S()

    def decode_stream(self, s):
        pass


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(remote=None, sv_text="管家打开记事本。", **cfg_kw):
    f = FinalASR.__new__(FinalASR)
    f.cfg = FinalASRConfig(**cfg_kw)
    f._rec = FakeSV(sv_text)
    f._pool = ThreadPoolExecutor(max_workers=1)
    events = []
    clock = Clock()
    f._setup_remote(remote, on_event=lambda name, msg, level="info", **d: events.append((name, level, d)), clock=clock)
    return f, events, clock


x = np.full(SR, 0.3, dtype=np.float32)
padded = SR + 2 * int(SR * PAD_SEC)

# ---------- 2. 远端正常 ----------
r = FakeRemote("管家打开记事本")
f, ev, _ = make(r)
res = f.transcribe_ex(x, fallback="流式")
check(isinstance(res, FinalResult) and res.text == "管家打开记事本" and res.backend == "fireredasr2", "remote result used")
check(res.conf == 0.97 and res.min_conf == 0.6 and res.fallback == "" and res.ms >= 0, "confidence + no fallback reason")
check(r.lens == [padded] and f._rec.seen == 0, "remote got padded audio; SenseVoice not touched")
check(ev == [], "no events on the happy path")
check(f.transcribe(x, fallback="流式") == "管家打开记事本", "transcribe() returns the same text")

# ---------- 3. 文字规整 ----------
f, _, _ = make(FakeRemote("我的 h a l o 音响声音太小了"))
check(f.transcribe_ex(x).text == "我的 halo 音响声音太小了", "spaced letters merged")
f, _, _ = make(FakeRemote("  "))
check(f.transcribe_ex(x, fallback="流式").text == "流式" and f.transcribe_ex(x).backend == "fireredasr2", "empty remote text -> stream fallback, no SenseVoice re-run")
check(f._rec.seen == 0, "SenseVoice not used for an empty remote answer")

# ---------- 4. 出错 → SenseVoice ----------
r = FakeRemote(fail=ConnectionError("refused"))
f, ev, _ = make(r)
res = f.transcribe_ex(x, fallback="流式")
check(res.backend == "sensevoice" and res.text == "管家打开记事本。" and res.fallback == "error", "error -> SenseVoice, reason=error")
check(len(ev) == 1 and ev[0][0] == "asr.fallback" and ev[0][2]["reason"] == "error", "asr.fallback event with reason")

# ---------- 5. 超时 → SenseVoice ----------
r = FakeRemote(delay=1.5)
f, ev, _ = make(r, remote_timeout=0.15)
t0 = time.perf_counter()
res = f.transcribe_ex(x, fallback="流式")
check(res.backend == "sensevoice" and res.fallback == "timeout", "slow remote -> timeout fallback")
check(time.perf_counter() - t0 < 1.0, "did not wait for the slow remote")
check(ev and ev[0][2]["reason"] == "timeout", "timeout event")

# ---------- 5b. 超时随语音时长放宽；服务活着时的超时不计入熔断 ----------
long_x = np.full(SR * 6, 0.3, dtype=np.float32)
f, ev, _ = make(FakeRemote(delay=0.35), remote_timeout=0.2, remote_timeout_per_sec=0.1)
check(abs(f.remote_timeout_for(6.0) - 0.5) < 1e-9 and f.remote_timeout_for(1.0) == 0.2 and f.remote_timeout_for(3.0) == 0.2,
      "timeout grows with speech beyond 3s only")
check(f.transcribe_ex(long_x).backend == "fireredasr2", "6s segment: 0.35s decode fits the stretched 0.5s timeout")
res = f.transcribe_ex(x)
check(res.backend == "sensevoice" and res.fallback == "timeout", "1s segment: the same delay times out")
r = FakeRemote(delay=0.4)  # /health 一直是好的
f, ev, _ = make(r, remote_timeout=0.1)
for _ in range(BREAKER_FAILS + 2):
    f.transcribe_ex(x)
check(f._open_until == 0.0 and f._fails == 0 and len(ev) == BREAKER_FAILS + 2, "slow-but-alive: every timeout falls back, none opens the breaker")
check(all(e[2]["counted"] is False for e in ev), "events say counted=False")
r.alive_val = False
for _ in range(BREAKER_FAILS):
    f.transcribe_ex(x)
check(f._open_until > 0, "timeouts while /health is dead do count and open the breaker")

# ---------- 6. 熔断 ----------
r = FakeRemote(fail=ConnectionError("down"))
f, ev, clock = make(r)
for _ in range(BREAKER_FAILS):
    f.transcribe_ex(x)
check(r.calls == BREAKER_FAILS, "remote tried until the breaker opened")
res = f.transcribe_ex(x)
check(r.calls == BREAKER_FAILS and res.backend == "sensevoice" and res.fallback == "breaker_open", "breaker open: remote not called")
n_ev = len(ev)
f.transcribe_ex(x)
f.transcribe_ex(x)
check(len(ev) == n_ev, "no per-segment event spam while the breaker is open")
check(any(e[1] == "warn" and e[2].get("reason") == "breaker_open" for e in ev), "one warn event when the breaker opens")

# ---------- 7. 探活恢复 ----------
r.alive_val = False
clock.t += 31
res = f.transcribe_ex(x)
check(r.calls == BREAKER_FAILS and res.fallback == "breaker_open", "cooldown over but still dead: stay open")
r.alive_val, r.fail = True, None
check(f.transcribe_ex(x).fallback == "breaker_open", "cooldown restarted after a failed probe")
clock.t += 31
res = f.transcribe_ex(x)
check(res.backend == "fireredasr2" and r.calls == BREAKER_FAILS + 1, "probe ok -> remote used again")
check(any(e[0] == "asr.backend" and e[2].get("state") == "recovered" for e in ev), "recovered event")
check(f.transcribe_ex(x).backend == "fireredasr2", "stays on remote afterwards")

# ---------- 8. 单次失败不熔断，成功后计数清零 ----------
r = FakeRemote(fail=ConnectionError("blip"))
f, ev, _ = make(r)
f.transcribe_ex(x)
f.transcribe_ex(x)
r.fail = None
f.transcribe_ex(x)
r.fail = ConnectionError("blip")
f.transcribe_ex(x)
f.transcribe_ex(x)
r.fail = None
check(f.transcribe_ex(x).backend == "fireredasr2", "failures must be consecutive to open the breaker")

# ---------- 9. 游戏态 ----------
r = FakeRemote()
f, ev, _ = make(r, gaming_free_mb=2048)
r.free_mb = 5000
f.enter_gaming(start_thread=False)
f.gaming_check()
check(r.controls == [] and f.transcribe_ex(x).backend == "fireredasr2", "plenty of free VRAM: keep the model loaded")
r.free_mb = 1200
f.gaming_check()
check(r.controls == ["unload"], "free VRAM < threshold -> unload requested")
res = f.transcribe_ex(x)
check(res.backend == "sensevoice" and res.fallback == "gpu_released", "after release: SenseVoice CPU")
f.gaming_check()
check(r.controls == ["unload"], "already released: no second unload")
f.leave_gaming(sync=True)
check(r.controls == ["unload", "load"], "leaving the game reloads the model")
check(f.transcribe_ex(x).backend == "fireredasr2", "back on the GPU model after reload")
check(any(e[0] == "asr.backend" and e[2].get("state") == "released" for e in ev), "released event")
check(any(e[0] == "asr.backend" and e[2].get("state") == "loaded" for e in ev), "loaded event")

# 游戏态下一直紧张（unload 失败）不抛异常
r2 = FakeRemote()
r2.control = lambda action, timeout=120.0: False
f, _, _ = make(r2)
r2.free_mb = 100
f.enter_gaming(start_thread=False)
f.gaming_check()
check(f.transcribe_ex(x).backend == "fireredasr2", "unload failed: keep using the model")

# ---------- 10. 只用 SenseVoice 的配置 ----------
f, ev, _ = make(None)
res = f.transcribe_ex(x, fallback="流式")
check(res.backend == "sensevoice" and res.text == "管家打开记事本。" and res.fallback == "" and ev == [], "no remote configured: plain SenseVoice, no events")
f.enter_gaming(start_thread=False)
f.gaming_check()
f.leave_gaming(sync=True)
check(True, "gaming hooks are no-ops without a remote")
check(make(None)[0].transcribe_ex(np.zeros(0, np.float32), fallback="流式").text == "流式", "empty audio -> fallback")
# ---------- 10b. 预热：听见有人开口时让 GPU 服务给显卡加热 ----------
r = FakeRemote()
f, ev, clock = make(r)
f.prewarm()
check(r.warmed.wait(2.0) and r.warms == 1, "prewarm asks the server to heat the GPU")
f.prewarm()
time.sleep(0.05)
check(r.warms == 1, "rate limited: a second prewarm within 1s is dropped")
clock.t += 1.5
r.warmed.clear()
f.prewarm()
check(r.warmed.wait(2.0) and r.warms == 2, "after the interval it warms again")
r = FakeRemote()
r.warm = lambda timeout=1.0: (_ for _ in ()).throw(ConnectionError("down"))
f, ev, _ = make(r)
f.prewarm()
time.sleep(0.1)
check(ev == [] and f._fails == 0, "a failing warm is silent: no event, no breaker count")
r = FakeRemote()
f, ev, clock = make(r)
f._gpu_released = True
f.prewarm()
time.sleep(0.05)
check(r.warms == 0, "model released (gaming): no warm")
r = FakeRemote(fail=ConnectionError("down"))
f, ev, clock = make(r)
for _ in range(BREAKER_FAILS):
    f.transcribe_ex(x)
f.prewarm()
time.sleep(0.05)
check(r.warms == 0, "breaker open: no warm")
make(None)[0].prewarm()
check(True, "no remote: prewarm is a no-op")

# ---------- 11. 真 HTTP 客户端 vs 本地假服务 ----------
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from guanjia.asr_final import FireRedClient

seen = {}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b"ok")
        elif self.path == "/stats":
            self._send(200, json.dumps({"loaded": True, "gpu_free_mb": 4321}).encode(), "application/json")
        else:
            self._send(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        if self.path == "/asr":
            seen["asr"] = np.frombuffer(body, dtype="<f4")
            if len(body) == 4:  # 约定：1 个样本 = 让服务端报错
                self._send(500, b"boom")
            else:
                self._send(200, json.dumps({"text": "你好", "conf": 0.9}, ensure_ascii=False).encode("utf-8"), "application/json")
        elif self.path == "/warm":
            seen["warm"] = True
            self._send(200, b"ok")
        elif self.path in ("/unload", "/load"):
            seen["ctl"] = self.path
            self._send(200, b"ok")
        else:
            self._send(404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}"
c = FireRedClient(url)
out = c.recognize(np.array([0.1, -0.2, 0.3], dtype=np.float32))
check(out["text"] == "你好" and np.allclose(seen["asr"], [0.1, -0.2, 0.3]), "client posts float32 LE PCM, parses JSON")
check(c.alive() and c.stats()["gpu_free_mb"] == 4321, "alive + stats")
check(c.warm() and seen.get("warm"), "warm posts /warm")
check(c.control("unload") and seen["ctl"] == "/unload" and c.control("load") and seen["ctl"] == "/load", "control unload/load")
try:
    c.recognize(np.array([0.5], dtype=np.float32))
    check(False, "500 must raise")
except Exception:
    check(True, "server error raises (caller falls back)")
srv.shutdown()
dead = FireRedClient("http://127.0.0.1:9")
check(not dead.alive() and dead.stats() is None and not dead.control("load", timeout=1.0) and not dead.warm(timeout=1.0),
      "dead server: alive False / stats None / control+warm False, no exception")
print("ALL PASS")
