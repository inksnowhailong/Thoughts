"""TTS 服务的游戏态显存看守（tts_server/tts_guard.py）：不加载模型，用假的显存读数验证决策。

背景：10-13 晚上主人反复开关游戏，语音每次都让 TTS 进程内 /unload + /load，内存一路涨到 13.8GB 被 OOM 杀三次。
新做法：游戏时空闲显存够就不动；真不够才让进程退出（内存归零），退出游戏后由启动器重新拉起。
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tts_server"))

fails = 0


def check(cond, what):
    global fails
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    if not cond:
        fails += 1


try:
    import tts_guard
    from tts_guard import EXIT_RELEASED, GamingGuard, rss_mb, trim_heap
except Exception as e:  # noqa: BLE001
    print(f"FAIL import tts_guard: {e!r}")
    sys.exit(1)


def wait_for(cond, sec=3.0):
    t0 = time.time()
    while time.time() - t0 < sec:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Fake:
    """假显存读数 + 假退出：free_seq 逐次取值，取完后停在最后一个。"""

    def __init__(self, free_seq):
        self.seq = list(free_seq)
        self.i = 0
        self.released = []
        self.logs = []

    def free_mb(self):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v

    def release(self, code, reason):
        self.released.append((code, reason))

    def guard(self, keep=3072, poll=0.03):
        return GamingGuard(self.free_mb, self.release, keep_free_mb=keep, poll_sec=poll, log=self.logs.append)


# 1. 退出码约定：75 = 为游戏腾显存主动退出（启动器据此等显存空出来再拉起）
check(EXIT_RELEASED == 75, "released exit code is 75 (EX_TEMPFAIL)")

# 2. rss_mb 读 /proc/self/status 的 VmRSS（kB -> MB）；读不到返回 None
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "status"
    p.write_text("Name:\tpython\nVmHWM:\t 7504724 kB\nVmRSS:\t 2735952 kB\n", encoding="utf-8")
    check(rss_mb(str(p)) == 2735952 // 1024, "rss_mb parses VmRSS in MB")
    check(rss_mb(str(Path(d) / "nope")) is None, "rss_mb returns None when unreadable")

# 3. trim_heap 永不抛异常（非 Linux 上返回 False）
check(trim_heap() in (True, False), "trim_heap never raises")

# 4. 空闲显存够：/unload 不动模型，返回 keep，看守线程在后台盯着
f = Fake([9000])
g = f.guard()
check(g.enter() == "keep", "plenty of free VRAM: enter() keeps the model")
time.sleep(0.2)
check(not f.released, "plenty of free VRAM: nothing released while watching")
check(g.watching, "plenty of free VRAM: watcher thread is running")
g.leave()
check(wait_for(lambda: not g.watching), "leave() stops the watcher")
check(not f.released, "leave() without pressure releases nothing")

# 5. 进入时就不够：立刻释放（返回 exit），带原因
f = Fake([1500])
g = f.guard()
check(g.enter() == "exit", "VRAM already tight: enter() asks the process to exit")
check(len(f.released) == 1 and f.released[0][0] == EXIT_RELEASED, "VRAM already tight: released once with code 75")
check("1500" in f.released[0][1] and "3072" in f.released[0][1], "release reason carries free and threshold MB")

# 6. 先够后不够（游戏逐步吃显存）：后台线程发现后释放，且只释放一次
f = Fake([9000, 9000, 8000, 1000, 500])
g = f.guard()
check(g.enter() == "keep", "ramp-up: starts as keep")
check(wait_for(lambda: len(f.released) == 1), "ramp-up: watcher releases once VRAM gets tight")
time.sleep(0.2)
check(len(f.released) == 1, "ramp-up: released exactly once")
check(wait_for(lambda: not g.watching), "ramp-up: watcher stops after release")

# 7. 游戏很快结束（leave 先到）：之后显存再紧也不会释放
f = Fake([9000, 9000, 9000, 100])
g = f.guard(poll=0.1)
g.enter()
g.leave()
time.sleep(0.5)
check(not f.released, "short game: leave() before pressure cancels the release")

# 8. keep_free_mb=0 表示关闭该功能（永不退出）
f = Fake([10])
g = f.guard(keep=0)
check(g.enter() == "off", "keep_free_mb=0 disables the guard")
check(not f.released and not g.watching, "disabled guard never watches or releases")

# 9. 读不到显存（nvidia 不可用）：不能误杀，保持
f = Fake([None, None, None])
g = f.guard()
check(g.enter() == "keep", "unreadable VRAM: keep")
time.sleep(0.2)
check(not f.released, "unreadable VRAM: never releases")
g.leave()

# 10. 连续 enter 不会起两个线程 / 重复释放
f = Fake([9000])
g = f.guard()
g.enter()
g.enter()
n = sum(1 for t in threading.enumerate() if t.name == "tts-gaming-watch")
check(n == 1, "enter() twice keeps a single watcher thread")
g.leave()
wait_for(lambda: not g.watching)

# 11. 读数函数抛异常也不能把看守线程带崩，更不能释放
calls = {"n": 0}


def boom():
    calls["n"] += 1
    raise RuntimeError("cuda error")


rel = []
g = GamingGuard(boom, lambda c, r: rel.append((c, r)), keep_free_mb=3072, poll_sec=0.03, log=lambda m: None)
check(g.enter() == "keep", "failing VRAM probe: keep")
time.sleep(0.2)
check(calls["n"] >= 2 and not rel, "failing VRAM probe: keeps polling, never releases")
g.leave()

# 12. 服务端接线（tts_server.py 启动即加载模型，无法 import，只检查关键接线）
src = (ROOT / "tts_server" / "tts_server.py").read_text(encoding="utf-8")
check("GamingGuard(" in src, "server: builds a GamingGuard")
check("guard.enter()" in src and "guard.leave()" in src, "server: /unload enters and /load leaves the guard")
check("force" in src, "server: in-process unload only on explicit ?force=1")
check("trim_heap()" in src, "server: in-process unload trims the heap")
check("rss" in src.lower() and "/stats" in src, "server: logs RSS and exposes /stats")

print("ALL PASS test_tts_guard" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
