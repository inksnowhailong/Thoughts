"""输入流看门狗 v2（保守版）：只在「确认的回调停摆」时动作，动作是让外层看门狗干净地重启整个进程。

2026-10-11 19:28 事故：launch_app 起 WeGame 后，语音进程再也没有产生任何 VAD 段，/status 一直是 LISTENING，
音频流对象还报告 active=True，但回调不再来。Windows 系统日志没有任何音频设备变化事件，所以只看流本身的行为。

v1（6804b86）在进程内重开流（sd._terminate()/_initialize()），并且上线后一出声就崩，被回滚（b504610）。
崩溃根因是主循环里 `t0, dur, g = self._play_events.popleft()` 把 `with self.guard as g` 的 g 覆盖成了整数，
下一轮 `g.stream` 抛 AttributeError，未捕获异常 + 看门狗每次重启都清空 voice-stderr.txt，所以什么痕迹都没有。
v2 的取舍：不在进程内重开流、不碰 sounddevice 私有 API、不持有流对象，只记证据 + 请求重启。

判据：
  callback_stall        回调超过 STALL_SEC 秒没来（正常每 10ms 一次）。输出和输入是同一条全双工流，回调停了输出也停了。
                        首次发现后还要再等 CONFIRM_SEC 秒、回调计数仍没动才算确认（防系统睡眠恢复 / 主线程被卡后的时钟跳变误判）
                        -> 动作：记 WARNING + audio.stall 事件，请求退出码 3 重启
  zero_during_playback  我们正在出声（真语音）时，输入连续累计 ZERO_PLAY_SEC 秒的帧一个非零样本都没有
                        -> 只记 audio.zero_playback 事件（限频），不动作；先攒证据，判据可靠了再升级
  selfcheck             launch_app 之后 SELFCHECK_DELAY 秒记一条 audio.selfcheck（回调还在不在、帧率），只记不动作

流（重）启动时 arm()，游戏态停流时 disarm()：停流期间没有回调是正常的。
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("guanjia.audio_watchdog")

FRAME_SEC = 0.01             # 流块长 10ms（VoiceDaemon.frame = sr // 100）
STALL_SEC = 5.0              # 回调多久没来算停摆
CONFIRM_SEC = 1.0            # 首次发现停摆后再等这么久、回调计数仍没动才确认
ZERO_PLAY_SEC = 6.0          # 出声期间输入连续全零累计多久，记一条事件
ZERO_EVENT_GAP_SEC = 300.0   # audio.zero_playback 事件最小间隔（限频）
SELFCHECK_MIN_RATE = 0.5     # 自检：实际帧数 / 期望帧数低于此记为 slow
SELFCHECK_STALL_SEC = 2.0    # 自检：回调多久没来记为 stalled


class InputWatchdog:
    """输入流健康判据。beat() 在音频回调线程里调（只写简单属性，很便宜），其余在主循环线程。"""

    def __init__(self, *, frame_sec: float = FRAME_SEC, stall_sec: float = STALL_SEC, confirm_sec: float = CONFIRM_SEC,
                 zero_play_sec: float = ZERO_PLAY_SEC, clock=time.monotonic) -> None:
        self.frame_sec, self.stall_sec, self.confirm_sec = frame_sec, stall_sec, confirm_sec
        self.zero_play_frames_max = round(zero_play_sec / frame_sec)
        self.clock = clock
        self.armed = False
        self.armed_at = 0.0
        self.last_beat = 0.0     # 最近一次回调的时刻
        self.last_signal = 0.0   # 最近一次输入里出现非零样本的时刻
        self.count = 0           # arm 以来的回调次数
        self.zero_frames = 0     # 出声帧里输入全零的累计帧数（一见非零样本清零）
        self._zero_reported = False  # 这一段全零是否已经报告过（见到信号后复位）
        self._suspect: tuple[float, int] | None = None  # (首次发现停摆的时刻, 当时的回调计数)

    @property
    def zero_play(self) -> float:
        return self.zero_frames * self.frame_sec

    def arm(self) -> None:
        """流（重）启动时调：重置基线，从此刻起计时。"""
        now = self.clock()
        self.armed_at = self.last_beat = self.last_signal = now
        self.count, self.zero_frames = 0, 0
        self._zero_reported, self._suspect = False, None
        self.armed = True

    def disarm(self) -> None:
        """游戏态停流时调：停流期间没有回调是正常的。"""
        self.armed = False
        self._suspect = None

    def beat(self, has_signal: bool, voiced: bool) -> None:
        """音频回调每帧调一次。has_signal：本帧输入有非零样本；voiced：本帧输出了真语音（不含保活噪声）。"""
        now = self.clock()
        self.last_beat = now
        self.count += 1
        if has_signal:
            self.last_signal = now
            self.zero_frames = 0
            self._zero_reported = False
        elif voiced:
            self.zero_frames += 1

    def evidence(self, why: str) -> dict:
        now = self.clock()
        return {"why": why, "cb_age": round(now - self.last_beat, 2), "frames": self.count,
                "signal_age": round(now - self.last_signal, 1), "zero_play": round(self.zero_play, 2),
                "since_arm": round(now - self.armed_at, 1)}

    def check(self) -> dict | None:
        """回调停摆判据。返回 None = 正常（或还没确认）；确认了返回带证据的 {"why": "callback_stall", ...}。"""
        if not self.armed:
            return None
        now = self.clock()
        if now - self.last_beat <= self.stall_sec:
            self._suspect = None
            return None
        if self._suspect is None or self._suspect[1] != self.count:
            self._suspect = (now, self.count)  # 第一次发现（或期间回调动过）：从现在起重新计确认时间
            return None
        if now - self._suspect[0] >= self.confirm_sec:
            return self.evidence("callback_stall")
        return None

    def zero_playback(self) -> dict | None:
        """出声期间输入恒零：每一段全零只报告一次（见到信号后才会再报）。只给事件用，不触发动作。"""
        if not self.armed or self._zero_reported or self.zero_frames < self.zero_play_frames_max:
            return None
        self._zero_reported = True
        return self.evidence("zero_during_playback")

    def mark(self) -> dict:
        """自检的起点：记下此刻的时间和回调计数。"""
        return {"t": self.clock(), "count": self.count, "armed_at": self.armed_at}

    def selfcheck(self, mark: dict) -> dict:
        """自检结果：回调还在不在（cb_age）、帧率够不够（rate = 实际帧数 / 期望帧数）。"""
        now = self.clock()
        ev = self.evidence("selfcheck")
        elapsed = max(now - mark["t"], 1e-6)
        ev["rate"] = round((self.count - mark["count"]) / (elapsed / self.frame_sec), 2)
        ev["rearmed"] = mark["armed_at"] != self.armed_at  # 自检期间流被重新启动过（游戏态）：rate 不可信
        ev["ok"] = self.armed and ev["cb_age"] <= SELFCHECK_STALL_SEC and ev["rate"] >= SELFCHECK_MIN_RATE
        return ev


class AudioGuard:
    """主循环每 0.5 秒调一次 tick()。只记证据 + 在确认停摆时返回 "stall"（调用方以退出码 3 退出，由外层看门狗拉起）。
    不持有流对象、不重开流、不碰 sounddevice。

    emit(comp, ev, msg, **kw) 即 elog.emit；describe() 返回设备名字典（启动时查好缓存，故障时不再去问 PortAudio）。
    """

    def __init__(self, watchdog: InputWatchdog, *, emit, describe=lambda: {}, clock=time.monotonic,
                 zero_event_gap: float = ZERO_EVENT_GAP_SEC) -> None:
        self.wd, self._emit, self._describe, self.clock = watchdog, emit, describe, clock
        self.zero_event_gap = zero_event_gap
        self._selfcheck: tuple[float, str, dict] | None = None  # (到期时刻, 原因, mark)
        self._last_zero_event = -1e9
        self._lock = threading.Lock()
        self.last_stall: dict | None = None  # 最近一次确认的停摆证据（/status 用）

    def request_selfcheck(self, reason: str, delay: float) -> None:
        """delay 秒后记一条 audio.selfcheck（任意线程可调；新请求覆盖旧请求）。只记不动作。"""
        with self._lock:
            self._selfcheck = (self.clock() + delay, reason, self.wd.mark())

    def tick(self, paused: bool) -> str:
        """paused：游戏态（流本来就停着）。返回 "ok" 或 "stall"（确认回调停摆，应以重启码退出）。"""
        with self._lock:
            pending = self._selfcheck
            if paused:
                self._selfcheck = None  # 游戏态：流停了又起，自检没有意义
                pending = None
            elif pending and self.clock() >= pending[0]:
                self._selfcheck = None
            else:
                pending = None
        if paused:
            return "ok"
        verdict = self.wd.check()
        if verdict is not None:
            self.last_stall = {**verdict, "at": time.strftime("%H:%M:%S")}
            names = self._describe()
            log.warning("输入流回调停摆：%.1fs 没有回调（frames=%d signal_age=%.1fs），请求重启进程（%s）",
                        verdict["cb_age"], verdict["frames"], verdict["signal_age"], names.get("in_name", "?"))
            self._emit("audio", "audio.stall", f"输入流回调停摆 {verdict['cb_age']}s，请求重启进程", level="error",
                       action="restart", **verdict, **names)
            return "stall"
        zero = self.wd.zero_playback()
        if zero is not None and self.clock() - self._last_zero_event >= self.zero_event_gap:
            self._last_zero_event = self.clock()
            self._emit("audio", "audio.zero_playback", f"出声期间输入连续 {zero['zero_play']}s 全零（只记录，不处理）",
                       level="warn", action="none", **zero, **self._describe())
        if pending is not None:
            ev = self.wd.selfcheck(pending[2])
            desc = dict(self._describe())
            if "rate" in desc:  # 设备采样率改名：ev["rate"] 是帧率比，两个 ** 展开同名会 TypeError、让语音进程退出
                desc["device_rate"] = desc.pop("rate")
            self._emit("audio", "audio.selfcheck", f"{pending[1]} 后输入流自检{'正常' if ev['ok'] else '异常'}",
                       level="info" if ev["ok"] else "warn", reason=pending[1], **ev, **desc)
        return "ok"

    def info(self) -> dict:
        """给 /status 和面板：当前回调年龄、帧数、最近一次确认的停摆。"""
        ev = self.wd.evidence("status")
        return {"cb_age": ev["cb_age"], "frames": ev["frames"], "signal_age": ev["signal_age"], "zero_play": ev["zero_play"],
                "armed": self.wd.armed, "last_stall": self.last_stall}
