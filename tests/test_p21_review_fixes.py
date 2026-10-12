"""P2.1 审查修复用例：唤醒词冷僻读音误命中、去唤醒词丢内容、/say 不算免喊、退出语音的退出码、记录脱敏、
审计跟随换文件/截短/缺文件、设置坏文件与写盘失败、事件流卡住的页面。不建音频流、不加载模型、不碰网络（只用本机回环）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p21_review_fixes.py
"""
import json
import logging
import queue
import socket
import tempfile
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import guanjia.control as control
import guanjia.voice as voice
from guanjia.audittail import AuditTail
from guanjia.barge import BargeConfig, BargeDetector
from guanjia.events import EventHub
from guanjia.panel import PanelBackend
from guanjia.settings import SettingsStore, from_dict
from guanjia.status import Health
from guanjia.turns import SayItem, TurnResult
from guanjia.wake import WakeMatcher, decide


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def raises(fn) -> str:
    try:
        fn()
    except ValueError as e:
        return str(e)
    return ""


# ---------- 1. 唤醒词 ----------
m = WakeMatcher(("管家",))
for miss in ("关节疼怎么办", "光顾一下", "这广告真烦", "关街那边"):
    check(m.find(miss) is None, f"rare reading of 家 does not hit: {miss}")
for hit in ("关家，开灯", "管加开灯", "光价开灯", "官家开灯"):
    check(m.find(hit) is not None, f"common mishearing still hits: {hit}")


def dd(text, score=0.6, min_chars=2):
    return decide(text, score, threshold=0.45, matcher=m, wake_required=True, min_chars=min_chars, in_window=False)


for said, sent in (("管家，哈尔滨明天下雨吗", "哈尔滨明天下雨吗"), ("管家，查一下附近的酒吧", "查一下附近的酒吧"),
                   ("管家，搜一下管家婆软件", "搜一下管家婆软件"), ("管家吧台灯打开", "吧台灯打开"),
                   ("管家呀，开灯", "开灯"), ("打开记事本，管家啊", "打开记事本"), ("管家管家，开灯", "开灯"),
                   ("管家，开灯，管家", "开灯"), ("管家，嗯嗯，那个，开灯吧", "那个，开灯吧")):
    r = dd(said)
    check(r.verdict == "accept" and r.text == sent, f"stripped content intact: {said} -> {r.text}")
check(dd("管家管家").verdict == "wake_only", "repeated wake word alone -> wake_only")
check(dd("管家", min_chars=3).verdict == "wake_only", "wake word alone still answered when min_chars=3")
check(dd("唉", min_chars=3).verdict == "ignored", "grunt still ignored when min_chars=3")
for sc in (0.4471, 0.28, 0.0):  # 声纹不再拦截：免喊窗口内低分照样放行
    r = decide("开灯", sc, threshold=0.45, matcher=m, wake_required=True, min_chars=2, in_window=True)
    check(r.verdict == "accept" and r.reason == "免喊窗口内" and r.text == "开灯", f"in window, score {sc} no longer rejected")
check("至少" in raises(lambda: from_dict({"wake_words": ["管"]})), "single-char wake word refused")
check("至少" in raises(lambda: from_dict({"wake_words": ["，，"]})), "punctuation-only wake word refused")
check(from_dict({"wake_words": "管家，小管"}).wake_words == ("管家", "小管"), "normal wake words still accepted")

# ---------- 2. 设置：坏文件不刷屏、写盘失败不炸 ----------
tmpdir = Path(tempfile.mkdtemp(prefix="gj_p21_fix_"))
bad = tmpdir / "voice-settings.json"
bad.write_text("{broken", encoding="utf-8")
warnings = []


class Grab(logging.Handler):
    def emit(self, record):
        warnings.append(record.getMessage())


logging.getLogger("guanjia.settings").addHandler(Grab())
store = SettingsStore(bad)
for _ in range(3):
    store.maybe_reload()
check(len([w for w in warnings if "读不了" in w]) == 1, "broken settings file warned once, not every 2 seconds")
blocked = tmpdir / "locked.json"
blocked.mkdir()  # 目标是个目录：os.replace 必然失败，模拟文件被别的程序占着
panel = PanelBackend(EventHub(), SettingsStore(blocked), status=dict, control=lambda a: (True, ""),
                     inject=lambda t, d: {})
ok, msg = panel.update_settings({"follow_up_sec": 5})
check(not ok and "写不进" in msg and not (tmpdir / "locked.tmp").exists(), "write failure reported, temp file cleaned")

# ---------- 3. 记录脱敏 ----------
hub = EventHub(records_file=tmpdir / "utterances.jsonl")
q = hub.subscribe()
uid = hub.new_utterance("mic", text="管家开灯")
hub.add_tool(uid, {"tool": "exec", "level": "L1", "decision": "allow", "state": "done",
                   "summary": 'exec: curl -H "Authorization: Bearer abcdefghijklmnop123" https://x'})
hub.finish(uid, reply="好的 sk-abcdefghijklmnop", error="token=abcdefgh12345678")
events = json.dumps([q.get_nowait() for _ in range(q.qsize())], ensure_ascii=False)
disk = (tmpdir / "utterances.jsonl").read_text(encoding="utf-8")
for where, blob in (("event stream", events), ("utterances.jsonl", disk)):
    check("abcdefghijklmnop" not in blob and "abcdefgh12345678" not in blob, f"no secrets in {where}")

# ---------- 4. 审计跟随 ----------
line = '{"kind":"tool","tool":"read","sessionKey":"agent:main:main","decision":"allow"}\n'
got = []
missing = tmpdir / "later" / "audit.jsonl"
tail = AuditTail(got.append, path=missing)
check(tail.poll() == 0, "missing audit file at start is fine")
missing.parent.mkdir()
missing.write_text(line, encoding="utf-8")
check(tail.poll() == 1, "file created after start: its first lines are read")
with missing.open("a", encoding="utf-8") as f:
    f.write(line + line)
check(tail.poll() == 2, "appended lines read")
missing.write_text(line, encoding="utf-8")  # 同一个文件被截短后写入新内容
check(tail.poll() == 1, "truncated file: new content read from the start")
fresh = tmpdir / "later" / "audit.new"
fresh.write_text(line + line, encoding="utf-8")
missing.unlink()
fresh.rename(missing)
check(tail.poll() == 2, "file replaced by a new one: new file read from the start")
with missing.open("a", encoding="utf-8") as f:
    f.write("[1, 2]\n42\n" + line)
check(tail.poll() == 1, "non-object JSON lines skipped without crashing")
boom = AuditTail(lambda e: None, path=missing, interval=0.05)
calls = [0]


def flaky():
    calls[0] += 1
    if calls[0] == 1:
        raise PermissionError("locked")
    return 0


boom.poll = flaky
boom._pos = 0
threading.Thread(target=boom._loop, daemon=True).start()
time.sleep(0.3)
boom.stop()
check(calls[0] >= 3, "tail thread survives a read error")


# ---------- 5. /say 不算免喊窗口 ----------
def make():
    d = object.__new__(voice.VoiceDaemon)
    d._lock, d._stop_evt = threading.Lock(), threading.Event()
    d._gen, d.state, d.sr = 0, "LISTENING", 16000
    d.play_q, d.text_q, d.in_q = queue.Queue(), queue.Queue(), queue.Queue()
    d._muted, d._paused = threading.Event(), threading.Event()
    d._play_buf, d._play_pos = np.zeros(0, dtype=np.float32), 0
    d._earcon = np.ones(4, dtype=np.float32)
    d._turn_first, d._turn_t0 = False, 0.0
    d.tray, d.last_error, d.sleep_state, d.gate_mode = None, "", "awake", "enforce"
    d.tts = SimpleNamespace(abort=lambda: None)
    d.brain = SimpleNamespace(last_run_id=None)
    d.health = Health(True, True)
    d.runtime = SimpleNamespace(state="ACTIVE", start=lambda: None, stop=lambda: None)
    d._exit_code = None
    d.hub = EventHub()
    d.store = SettingsStore(None)
    d.wake = WakeMatcher(("管家",))
    d.gate = None
    d.barge = BargeDetector(BargeConfig())
    d._ack_text = ""
    d._followup_until = 0.0
    return d


d = make()
seen = {}


class SayRunner:
    def run_say(self, text, gen):
        seen["say_window"] = d._in_followup()
        seen["say_verdict"] = d._on_heard("周老师点的不一样吗", 0.6, source="mic")["verdict"]
        d.stop_speaking("barge-in")
        seen["after_barge"] = d._followup_until
        return TurnResult(reply=text, spoken=[text])

    def run_reply(self, text, gen):
        seen["reply_window"] = d._in_followup()
        seen["backchannel"] = d._on_heard("好", 0.6, source="mic")["verdict"]
        return TurnResult(reply="要我打开吗", spoken=["要我打开吗"])


d.runner = SayRunner()
d._handle_item(SayItem("微信：晚上回来吃饭吗", None))
check(seen["say_window"] is False and seen["say_verdict"] == "rejected", "while /say is reading, no wake word -> rejected")
check(seen["after_barge"] == 0.0, "barge-in on /say does not open the follow-up window")
d._handle_item("打开记事本")
check(seen["reply_window"] is True, "during a real conversation turn the window still counts")
check(seen["backchannel"] == "ignored", "a lone 好 while it is still talking is a backchannel, not a new turn")
check(d._on_heard("好", 0.6, source="mic")["verdict"] == "accept", "after it finished, a lone 好 answers its question")


# ---------- 6. 退出语音：退出码必须是 0（看门狗据此不再拉起） ----------
class FakeStream:
    active, latency = True, (0.01, 0.01)

    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


voice.sd.Stream = FakeStream
voice.start_control_server = lambda api, port, panel=None: SimpleNamespace(shutdown=lambda: None)


def run_once(delay: float) -> int:
    d = make()
    d.in_dev = d.out_dev = None
    d.mic_gain, d.aec, d.control_port, d.frame, d.tray_on, d.panel = 1.0, None, 0, 160, False, None
    d.monitor = d.audit = SimpleNamespace(start=lambda: None, stop=lambda: None)
    d._front_dirty, d._play_events, d._turn_marks, d._dropped, d._underflows = False, deque(), None, 0, 0
    codes = []

    def target():
        try:
            d.run()
        except SystemExit as e:
            codes.append(e.code)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    time.sleep(delay)
    d._panel_control("quit")
    t.join(10)
    return codes[0] if codes else -99


codes = [run_once(0.6 + 0.09 * i) for i in range(6)]
check(codes == [0] * 6, f"panel quit always exits with code 0 (got {codes})")


# ---------- 7. 事件流：卡住不读的页面不拖慢事件总线，过一会儿让出名额 ----------
control.SSE_WRITE_TIMEOUT_SEC = 1.0
hub = EventHub()
panel = PanelBackend(hub, SettingsStore(None), status=dict, control=lambda a: (True, ""), inject=lambda t, d: {},
                     token="t" * 32)
srv = control.make_control_server(SimpleNamespace(status=dict), port=0, panel=panel)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
stuck = socket.socket()
stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
stuck.connect(("127.0.0.1", port))
stuck.sendall(f"GET /api/events?token={'t' * 32} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
time.sleep(0.3)
check(len(hub._subs) == 1, "stuck page subscribed")
t0 = time.monotonic()
for i in range(3000):
    hub.log("INFO", "other", f"{i} " + "x" * 4000)
check(time.monotonic() - t0 < 2.0, "emitting 3000 big log lines with a stuck page stays fast")
deadline = time.monotonic() + 15
while hub._subs and time.monotonic() < deadline:
    hub.log("INFO", "other", "y" * 4000)
    time.sleep(0.05)
check(not hub._subs, "stuck page dropped after the write timeout")
url = f"http://127.0.0.1:{port}/api/snapshot?token={'t' * 32}"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走系统代理
check(opener.open(url, timeout=3).status == 200, "server still answers after dropping the stuck page")
stuck.close()
srv.shutdown()

print("ALL PASS test_p21_review_fixes")
