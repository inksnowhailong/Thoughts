"""P2 上线前审查补的用例：念出来的数字/时间/网址、整轮念不出的记账、控制口 Host 校验、/say 排队上限、
上一轮还没出声时新一句仍会换代（审查第 1 条本次不改）、静音前导不算"在出声"。不建音频流、不加载模型、不联网。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p2_review_fixes.py
"""
import http.client
import json
import queue
import threading
from types import SimpleNamespace

import numpy as np

import guanjia.voice as voice
from guanjia.control import make_control_server
from guanjia.textseg import SentenceSplitter, speakable
from guanjia.turns import SayItem, TurnRunner


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def split(pieces: list[str]) -> list[str]:
    sp = SentenceSplitter(first_hard_max_chars=20)
    out = [s for p in pieces for s in sp.push(p)]
    return out + list(sp.flush())


# ---------- 1. 念出来的内容 ----------
check(speakable("今天晴，18~25度。") == "今天晴，18到25度。", "range 18~25 reads as 18 dao 25")
check(speakable("大概3 ～ 5分钟") == "大概3到5分钟", "fullwidth range with spaces")
check(split(["好，明天早上9:30提醒你交电费。"]) == ["好，明天早上9:30提醒你交电费。"], "time 9:30 not split")
check(split(["好，明天早上9:", "30提醒你交电费。"]) == ["好，明天早上9:30提醒你交电费。"], "time split across stream pieces")
check(split(["共计1,000元，", "已经记下了。"])[0] == "共计1,000元，", "thousands comma not split")
check(split(["详见 https:", "//a.cn。"]) == ["详见 https://a.cn。"] and speakable("详见 https://a.cn。") == "详见",
      "URL not cut at its colon, then removed")
check(split(["提醒：明天带伞。"]) == ["提醒：明天带伞。"], "fullwidth colon behaviour unchanged")

# ---------- 2. 整轮念不出 / 断流时的半句都要记账 ----------
def runner(stream, gen=lambda: 0):
    return TurnRunner(open_stream=stream, synth=lambda s: (np.zeros(10, dtype=np.float32), 16000),
                      enqueue=lambda g, p, sr: None, current_gen=gen)


r = runner(lambda t: iter(["| --- | --- |"])).run_reply("x", 0)
check(not r.spoken and r.unspoken and r.error is None, "reply with nothing speakable is recorded as unspoken")
r = runner(lambda t: iter(["NO_REPLY"])).run_reply("x", 0)
check(not r.unspoken, "NO_REPLY stays silent without an unspoken record")


def drop_mid_sentence(t):
    yield "今天晴"
    raise ConnectionError("gateway dropped")


r = runner(drop_mid_sentence).run_reply("x", 0)
check(r.error and r.unspoken == ["今天晴"], "half sentence left in the splitter is recorded when the stream breaks")

# ---------- 3. 控制口 ----------
class Api:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)
        return True, "queued"

    def status(self):
        return {"view": "listening"}

    def set_sleep_state(self, s):
        return True


srv = make_control_server(Api(), port=0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    c.request(method, path, body=body, headers=headers or {})
    resp = c.getresponse()
    out = resp.status, json.loads(resp.read() or b"{}")
    c.close()
    return out


try:
    check(req("GET", "/status")[0] == 200, "normal local GET still works")
    check(req("GET", "/status", headers={"Host": "evil.example:18795"}) == (403, {"ok": False, "reason": "bad_host"}),
          "DNS-rebinding Host is refused")
    check(req("GET", "/status", headers={"Host": f"localhost:{port}"})[0] == 200, "Host localhost accepted")
    check(req("POST", "/say", "{}", {"Content-Type": "application/json", "Content-Length": "abc"})[0] == 400,
          "bad Content-Length answered, no traceback")
finally:
    srv.shutdown()
    srv.server_close()

# ---------- 4. 守护进程接线 ----------
d = object.__new__(voice.VoiceDaemon)
d._lock, d._stop_evt = threading.Lock(), threading.Event()
d._gen, d.state = 0, "LISTENING"
d.play_q, d.text_q = queue.Queue(), queue.Queue()
d._muted, d._paused = threading.Event(), threading.Event()
d._play_buf, d._play_pos, d._buf_gen, d._played_gen = np.zeros(0, dtype=np.float32), 0, 0, -1
d._earcon = np.ones(4, dtype=np.float32)
d._turn_first, d._turn_t0 = False, 0.0
d.tts = SimpleNamespace(abort=lambda: None)

results = [d.say(f"第{i}段") for i in range(voice.SAY_MAX_PENDING + 1)]
check(all(r == (True, "queued") for r in results[:-1]) and results[-1] == (False, "busy"), "/say refuses past the pending cap")
while not d.text_q.empty():
    d.text_q.get_nowait()

# 审查建议第 1 条本次不改：正在等模型时新一句仍换代停播
d.state, d._turn_first = "SPEAKING", True
d._accept_utterance("要不要带伞")
check(d._gen == 1, "new utterance while waiting for the model still stops playback")
d.state, d._turn_first = "SPEAKING", False
d._accept_utterance("好了好了")
check(d._gen == 2, "utterance while the reply is audible still stops playback")

# 音频回调：150ms 静音前导不算"在出声"，真声才算
sr = 16000
d.sr, d.frame = sr, sr // 100
d._noise, d._noise_pos = np.zeros(sr, dtype=np.float32), 0
d._play_events, d.in_q = voice.deque(maxlen=10), queue.Queue()
d._underflows, d._dropped = 0, 0
while not d.play_q.empty():
    d.play_q.get_nowait()
d.play_q.put((d._gen, np.concatenate([np.zeros(sr * 15 // 100, dtype=np.float32), np.full(sr // 10, 0.1, dtype=np.float32)])))
flags = []
for _ in range(25):
    outdata = np.zeros((d.frame, 1), dtype=np.float32)
    d._callback(np.zeros((d.frame, 1), dtype=np.float32), outdata, d.frame, None, SimpleNamespace(output_underflow=False))
    flags.append(d.in_q.get_nowait().playing)
check(not any(flags[:15]) and all(flags[15:25]), "lead silence frames are not 'playing', voiced frames are")

print("ALL PASS test_p2_review_fixes")
