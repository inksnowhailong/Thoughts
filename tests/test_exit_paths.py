"""退出路径必须留痕：任何一条让语音进程退出的路径，都要先记 voice.exit 事件 + 一行 WARNING 日志（含原因）。

2026-10-11/12 事故：进程以退出码 1 退出，voice.log 没有最后一行、事件流没有 voice.exit、voice-stderr.txt 是空的。
原因是「未捕获异常」只会把堆栈打到 stderr，而 stderr 文件每次被看门狗拉起新进程时都会被清空，堆栈就丢了。

  1. 未捕获异常 / 线程崩溃：sys.excepthook / threading.excepthook 记日志 + voice.exit / thread.crash 事件
  2. 端到端（子进程跑真 main()）：run() 里抛异常 -> 退出码 1，事件流有 voice.exit(uncaught_exception)，
     voice.log 有 WARNING 以上的一行带原因，stderr 有完整堆栈
  3. 原生崩溃：faulthandler 把 Python 栈打到 stderr
  4. 托盘 / 面板的退出请求也是一行 WARNING（带原因）

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_exit_paths.py
"""
import contextlib
import io
import json
import logging
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import guanjia.voice as voice
from guanjia import elog

REPO = Path(__file__).resolve().parents[1]


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def events(dirpath, *evs):
    out = [json.loads(ln) for f in sorted(Path(dirpath).glob("events-*.jsonl")) for ln in f.read_text(encoding="utf-8").splitlines()]
    return [e for e in out if not evs or e["ev"] in evs]


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ---------- 1. 进程内：excepthook ----------
cap = Capture()
logging.getLogger().addHandler(cap)
old_hooks = (sys.excepthook, threading.excepthook)
with tempfile.TemporaryDirectory() as dirpath:
    log = elog.init("voice", log_dir=Path(dirpath), sync=True)
    try:
        voice.install_crash_hooks()
        try:
            raise RuntimeError("boom-main")
        except RuntimeError:
            info = sys.exc_info()
        with contextlib.redirect_stderr(io.StringIO()):  # 默认钩子会把堆栈打到 stderr，测试里别刷屏
            sys.excepthook(*info)
        ex = events(dirpath, "voice.exit")
        check(len(ex) == 1 and ex[0]["data"]["code"] == voice.EXIT_CRASH and ex[0]["data"]["reason"] == "uncaught_exception"
              and ex[0]["level"] == "error", "uncaught exception -> voice.exit(code=1, reason=uncaught_exception)")
        check("RuntimeError: boom-main" in ex[0]["data"]["error"], "voice.exit carries the exception text")
        warn = [r for r in cap.records if r.levelno >= logging.WARNING and "uncaught_exception" in r.getMessage()]
        check(len(warn) == 1 and warn[0].exc_info is not None, "one WARNING+ log line with the reason and the traceback")

        def boom() -> None:
            raise ValueError("boom-thread")

        t = threading.Thread(target=boom, name="frontend")
        t.start()
        t.join()
        tc = events(dirpath, "thread.crash")
        check(len(tc) == 1 and tc[0]["data"]["thread"] == "frontend" and "boom-thread" in tc[0]["data"]["error"],
              "thread crash -> thread.crash event with thread name")
        check(any(r.levelno >= logging.ERROR and "frontend" in r.getMessage() for r in cap.records), "thread crash -> ERROR log line")

        # 4. 托盘 / 面板退出请求：一行 WARNING 带原因
        d = object.__new__(voice.VoiceDaemon)
        d._exit_code, d._stop_evt = None, threading.Event()
        cap.records.clear()
        d.request_exit(voice.EXIT_RESTART, "tray")
        w = [r for r in cap.records if r.levelno >= logging.WARNING and "exit requested" in r.getMessage()]
        check(len(w) == 1 and "code=3" in w[0].getMessage() and "reason=tray" in w[0].getMessage(),
              "request_exit -> WARNING 'exit requested: code=3 reason=tray'")
    finally:
        sys.excepthook, threading.excepthook = old_hooks
        logging.getLogger().removeHandler(cap)
        log.close()
        elog._default = elog.NullLog()

# ---------- 2. 端到端：子进程跑真 main()，run() 里抛异常 ----------
BOOM_MAIN = r"""
import sys, functools
import guanjia.voice as voice
from guanjia import elog

tmp, MODE = sys.argv[1], sys.argv[2]
elog.init = functools.partial(elog.init, log_dir=__import__("pathlib").Path(tmp), sync=True)  # 事件写进临时目录，不碰线上日志


class Boom:
    def __init__(self, **kw): pass

    def run(self):
        if MODE == "exc":
            raise RuntimeError("boom-run")
        import faulthandler
        faulthandler._sigsegv()


voice.VoiceDaemon = Boom
sys.argv = ["voice", "-v", "--log-file", tmp + "/voice.log"]
voice.main()
"""

for mode in ("exc", "segv"):
    with tempfile.TemporaryDirectory() as dirpath:
        p = subprocess.run([sys.executable, "-c", BOOM_MAIN, dirpath, mode], cwd=REPO, capture_output=True, timeout=120,
                           env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        err = p.stderr.decode("utf-8", "replace")
        vlog = (Path(dirpath) / "voice.log").read_text(encoding="utf-8") if (Path(dirpath) / "voice.log").exists() else ""
        if p.returncode != (1 if mode == "exc" else p.returncode):
            print("---- stderr ----\n" + err[-3000:] + "\n---- voice.log ----\n" + vlog[-3000:])
        if mode == "exc":
            check(p.returncode == 1, f"e2e: exception in run() -> exit code 1 (got {p.returncode})")
            ex = events(dirpath, "voice.exit")
            check(len(ex) == 1 and ex[0]["data"]["reason"] == "uncaught_exception" and ex[0]["data"]["code"] == 1,
                  "e2e: voice.exit event written before the process dies")
            check(any(("WARNING" in ln or "ERROR" in ln or "CRITICAL" in ln) and "uncaught_exception" in ln for ln in vlog.splitlines()),
                  "e2e: voice.log has a WARNING+ line with the reason")
            check("RuntimeError: boom-run" in vlog, "e2e: voice.log has the traceback")
            check("Traceback" in err and "RuntimeError: boom-run" in err, "e2e: stderr (voice-stderr.txt) has the traceback too")
        else:
            check(p.returncode != 0, f"e2e: native crash -> non-zero exit (got {p.returncode})")
            check("fatal python error" in err.lower() and "most recent call first" in err.lower(),
                  "e2e: faulthandler prints the Python stack to stderr on a native crash")

print("ALL PASS test_exit_paths")
