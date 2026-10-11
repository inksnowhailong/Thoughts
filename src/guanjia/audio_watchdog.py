"""输入流看门狗：音频流"悄悄断了"（进程活着、s.active 还是 True，但回调不再来 / 麦克一直是死的）时自动重开。

2026-10-11 19:28 事故：launch_app 起 WeGame 后，语音进程再也没有产生任何 VAD 段，/status 一直是 LISTENING。
Windows 系统日志在这段时间没有任何音频设备变化事件，所以靠"设备变了"来判断抓不到；这里只看流本身的行为：

  callback_stall        回调超过 STALL_SEC 秒没来（正常每 10ms 一次）。输出和输入是同一条全双工流，回调停了输出也停了
  stream_inactive       流对象自己报告不活跃（非游戏态）
  zero_during_playback  我们正在出声（真语音）时，输入连续累计 ZERO_PLAY_SEC 秒的帧一个非零样本都没有。
                        安静时音箱噪声门把输入置成数字零是正常的，所以"全零"本身不是判据；但我们出声时，
                        音箱自己的回声一定会漏进麦（线上 251 个 1 秒窗口的打断监视日志里，出声期间峰值最小也 > 0），
                        出声却连续全零说明输入通路是死的
  callback_slow         （只有 launch_app 后的自检用）回调还在来，但帧率低于期望的一半

只看"没有数据"和"出声时仍全零"，不看"安静时全零"，所以正常安静、正常播放都不会误触发；游戏态停流期间整个看门狗不工作。

结构：
  InputWatchdog   纯逻辑 + 注入时钟：回调线程调 beat()，主循环调 check()/selfcheck()
  StreamGuard     拥有音频流：开 / 重开（关旧流有超时，不会被卡死的驱动拖住）/ 频率上限 / 记事件；主循环调 tick()
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("guanjia.audio_watchdog")

FRAME_SEC = 0.01             # 流块长 10ms（VoiceDaemon.frame = sr // 100）
STALL_SEC = 5.0              # 回调多久没来算停了
ZERO_PLAY_SEC = 6.0          # 出声期间输入连续全零累计多久算死
SELFCHECK_STALL_SEC = 2.0    # launch_app 后自检：回调多久没来算停（比常驻看门狗更紧）
SELFCHECK_MIN_RATE = 0.5     # launch_app 后自检：实际帧数 / 期望帧数低于此算慢
SELFCHECK_MIN_WINDOW = 5.0   # 自检窗口短于此不判（样本太少）
MAX_REOPENS = 3              # REOPEN_WINDOW_SEC 内最多重开这么多次，再失败就整进程重启
REOPEN_WINDOW_SEC = 600.0
CLOSE_TIMEOUT_SEC = 3.0      # 关旧流最多等这么久；卡死的驱动不能拖住主循环


class InputWatchdog:
    """输入流健康判据。beat() 在音频回调线程里调（只写简单属性，很便宜），其余在主循环线程。"""

    def __init__(self, *, frame_sec: float = FRAME_SEC, stall_sec: float = STALL_SEC, zero_play_sec: float = ZERO_PLAY_SEC,
                 selfcheck_stall_sec: float = SELFCHECK_STALL_SEC, selfcheck_min_rate: float = SELFCHECK_MIN_RATE,
                 clock=time.monotonic) -> None:
        self.frame_sec, self.stall_sec = frame_sec, stall_sec
        self.zero_play_frames_max = round(zero_play_sec / frame_sec)
        self.selfcheck_stall_sec, self.selfcheck_min_rate = selfcheck_stall_sec, selfcheck_min_rate
        self.clock = clock
        self.armed = False
        self.epoch = 0           # 每次 arm 加一：自检的 mark 据此判断"这条流后来是不是被重开过"
        self.armed_at = 0.0
        self.last_beat = 0.0     # 最近一次回调的时刻
        self.last_signal = 0.0   # 最近一次输入里出现非零样本的时刻
        self.count = 0           # arm 以来的回调次数
        self.zero_frames = 0     # 出声帧里输入全零的累计帧数（一见非零样本清零）

    @property
    def zero_play(self) -> float:
        return self.zero_frames * self.frame_sec

    def arm(self) -> None:
        """流（重）启动时调：重置基线，从此刻起计时。"""
        now = self.clock()
        self.epoch += 1
        self.armed_at = self.last_beat = self.last_signal = now
        self.count, self.zero_frames = 0, 0
        self.armed = True

    def disarm(self) -> None:
        """游戏态停流时调：停流期间没有回调是正常的。"""
        self.armed = False

    def beat(self, has_signal: bool, voiced: bool) -> None:
        """音频回调每帧调一次。has_signal：本帧输入有非零样本；voiced：本帧输出了真语音（不含保活噪声）。"""
        now = self.clock()
        self.last_beat = now
        self.count += 1
        if has_signal:
            self.last_signal = now
            self.zero_frames = 0
        elif voiced:
            self.zero_frames += 1

    def _evidence(self, why: str, now: float) -> dict:
        return {"why": why, "cb_age": round(now - self.last_beat, 2), "frames": self.count,
                "signal_age": round(now - self.last_signal, 1), "zero_play": round(self.zero_play, 2),
                "since_arm": round(now - self.armed_at, 1)}

    def check(self, stream_active: bool) -> dict | None:
        """常驻判据。返回 None = 正常；否则返回带证据的判定 {"why": ..., ...}。"""
        if not self.armed:
            return None
        now = self.clock()
        if not stream_active:
            return self._evidence("stream_inactive", now)
        if now - self.last_beat > self.stall_sec:
            return self._evidence("callback_stall", now)
        if self.zero_frames >= self.zero_play_frames_max:
            return self._evidence("zero_during_playback", now)
        return None

    def mark(self) -> dict:
        """自检的起点：记下此刻的时间、回调计数和流代次。"""
        return {"t": self.clock(), "count": self.count, "epoch": self.epoch}

    def selfcheck(self, mark: dict, stream_active: bool) -> dict | None:
        """launch_app 之后的一次性自检：比常驻判据更紧（回调 2 秒没来、或帧率不到期望一半）。
        从 mark 之后流被重开过（epoch 变了）或窗口太短都不判。"""
        if not self.armed or mark["epoch"] != self.epoch:
            return None
        now = self.clock()
        elapsed = now - mark["t"]
        if elapsed < SELFCHECK_MIN_WINDOW:
            return None
        if not stream_active:
            return self._evidence("stream_inactive", now)
        if now - self.last_beat > self.selfcheck_stall_sec:
            return self._evidence("callback_stall", now)
        got, expected = self.count - mark["count"], elapsed / self.frame_sec
        if got < self.selfcheck_min_rate * expected:
            ev = self._evidence("callback_slow", now)
            ev.update(got=got, expected=round(expected), rate=round(got / expected, 2))
            return ev
        return None


class StreamGuard:
    """拥有音频流（上下文管理器：进入 = 打开并启动，退出 = 关闭）；主循环每 0.5 秒调一次 tick()。

    流对象只需要支持 with 协议（真 sd.Stream 就是这样）。make_stream() 每次返回一条新流；
    refresh() 在旧流正常关掉后调用（让 PortAudio 重新枚举设备，拿到新的默认设备）；
    describe() 返回设备名字典，随事件一起记；emit(comp, ev, msg, **kw) 即 elog.emit；
    on_event(kind, data) 给托盘 / 状态用（kind: stall / reopened / failed）。
    tick() 的返回值：ok / deferred / reopened / failed（failed = 调用方应以重启退出码退出，让看门狗拉起整个进程）。
    """

    def __init__(self, make_stream, watchdog: InputWatchdog, *, emit, describe=lambda: {}, refresh=None, on_event=None,
                 clock=time.monotonic, max_reopens: int = MAX_REOPENS, window_sec: float = REOPEN_WINDOW_SEC,
                 close_timeout: float = CLOSE_TIMEOUT_SEC) -> None:
        self._make, self.wd, self._emit, self._describe = make_stream, watchdog, emit, describe
        self._refresh, self._on_event, self.clock = refresh, on_event, clock
        self.max_reopens, self.window_sec, self.close_timeout = max_reopens, window_sec, close_timeout
        self.stream = None
        self._history: list[float] = []   # 近期重开的时刻
        self._total = 0
        self._last: dict | None = None
        self._selfcheck: tuple[float, str, dict] | None = None  # (到期时刻, 原因, mark)
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def __enter__(self) -> "StreamGuard":
        self.stream = self._make()
        self.stream.__enter__()
        self.wd.arm()
        return self

    def __exit__(self, *exc) -> bool:
        self.wd.disarm()
        if self.stream is not None:
            self._close(self.stream)
        return False

    def _close(self, stream) -> bool:
        """关流，最多等 close_timeout 秒。卡死的驱动会让 close 永远不返回，那就丢给后台线程放弃它，返回 False。"""
        done = threading.Event()

        def work() -> None:
            try:
                stream.__exit__(None, None, None)
            except Exception:  # noqa: BLE001  关不干净也要继续重开
                log.exception("close audio stream failed")
            finally:
                done.set()

        threading.Thread(target=work, name="audio-close", daemon=True).start()
        return done.wait(self.close_timeout)

    # ---------- 自检 ----------
    def request_selfcheck(self, reason: str, delay: float) -> None:
        """delay 秒后在主循环里自检一次输入流（任意线程可调；新请求覆盖旧请求）。"""
        with self._lock:
            self._selfcheck = (self.clock() + delay, reason, self.wd.mark())

    # ---------- 主循环 ----------
    def tick(self, paused: bool, busy: bool) -> str:
        """paused：游戏态（流本来就停着）；busy：我们正在出声（只影响 zero_during_playback 的重开时机）。"""
        with self._lock:
            pending, due = self._selfcheck, False
            if paused:
                self._selfcheck = None  # 游戏态：流停了又起，自检没有意义
            elif pending and self.clock() >= pending[0]:
                self._selfcheck, due = None, True
        if paused or self.stream is None:
            return "ok"
        trigger = "watchdog"
        verdict = self.wd.check(self.stream.active)
        if verdict is None and due:
            trigger = pending[1]
            verdict = self.wd.selfcheck(pending[2], self.stream.active)
            if verdict is None:
                self._emit("audio", "audio.selfcheck", f"{trigger} 后输入流自检正常", ok=True, reason=trigger,
                           frames=self.wd.count, signal_age=round(self.clock() - self.wd.last_signal, 1), **self._describe())
        if verdict is None:
            return "ok"
        if verdict["why"] == "zero_during_playback" and busy:
            return "deferred"  # 重开会截断正在念的话；念完再说（回调停了的情形没有声音可截，不延后）
        return self._handle(verdict, trigger)

    def _handle(self, verdict: dict, trigger: str) -> str:
        names = self._describe()
        self._emit("audio", "audio.stall", f"输入流异常：{verdict['why']}（{names.get('in_name', '?')}）", level="warn",
                   trigger=trigger, **verdict, **names)
        self._last = {"why": verdict["why"], "trigger": trigger, "at": time.strftime("%H:%M:%S")}
        self._notify("stall", verdict)
        now = self.clock()
        self._history = [t for t in self._history if now - t < self.window_sec]
        if len(self._history) >= self.max_reopens:
            self._emit("audio", "audio.reopen_failed", f"{self.window_sec:.0f} 秒内已重开 {len(self._history)} 次仍不行，整进程重启",
                       level="error", reason="too_many", attempts=len(self._history), **names)
            self._notify("failed", {"reason": "too_many"})
            return "failed"
        self._history.append(now)
        t0 = time.monotonic()
        closed = self._close(self.stream)
        try:
            if closed and self._refresh is not None:
                self._refresh()
            new = self._make()
            new.__enter__()
        except Exception as e:  # noqa: BLE001  重开失败：交给上层以重启退出码退出
            self._emit("audio", "audio.reopen_failed", f"重开输入流失败：{e}", level="error", reason="open_failed",
                       error=f"{type(e).__name__}: {e}", closed_clean=closed, **names)
            self._notify("failed", {"reason": "open_failed"})
            return "failed"
        self.stream = new
        self.wd.arm()
        self._total += 1
        self._emit("audio", "audio.reopen", f"输入流已重开（{names.get('in_name', '?')}）", attempt=len(self._history),
                   closed_clean=closed, ms=round((time.monotonic() - t0) * 1000), why=verdict["why"], trigger=trigger,
                   **self._describe())
        self._notify("reopened", verdict)
        return "reopened"

    def _notify(self, kind: str, data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, data)
            except Exception:  # noqa: BLE001  托盘刷新出错不能影响重开
                log.exception("audio event hook failed")

    def info(self) -> dict:
        """给 /status 和面板：累计重开次数与最近一次故障。"""
        return {"reopens": self._total, "last": self._last}
