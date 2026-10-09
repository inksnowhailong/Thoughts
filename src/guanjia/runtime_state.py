"""运行态状态机（D25 开机才用 / D26 游戏态彻底静默）。

状态：
- OFFLINE：监控未启动或已停止（守护进程不在线）。
- IDLE：在线但处于静默期（开机后 / 游戏结束后 5 分钟），被动应答照常，主动侧不开口。
- ACTIVE：在线且静默期已过，主动侧可以开口。
- GAMING：游戏中，守护进程停麦、停播、释放模型，不出声不弹窗。

检测（后台线程每 2s 一次，优先级从高到低，任一命中即判为"在玩"）：
1. 进程白名单（games.json，可编辑）；
2. 前台窗口独占全屏 + GPU 利用率 > 60%，持续 10s；
3. 手动开关（"我打游戏了"）。
退出 GAMING 需要"在玩"信号连续消失 60s（滞后，防切窗口/读条抖动）；手动关闭则立即退出。
手动"我打游戏了"有两条非语音退出口，都不需要用户再开口：
1. 超时：默认 3 小时（MANUAL_GAMING_TIMEOUT_S）后自动关闭手动标记，回落到正常信号判定；
2. 进程消失：手动态期间若曾检测到白名单进程，随后又检测不到（用户确实退出了游戏），
   同样自动关闭手动标记，仍沿用 60s 滞后退出 GAMING（不是立即退出）。

离线区间（D25）：开机时把"上次心跳 → 本次启动"写成一条"离线 从…到…"；游戏时段结束后同样补写一条。
心跳文件 data/last-seen.txt 每 30s 更新一次（关机钩子的简化方案：不注册系统关机事件，靠心跳推断关机时间）。

自测：python -m guanjia.runtime_state --selftest（注入假时钟/假探测，不碰音频、不写真实时间线）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("guanjia.runtime")

REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "data"
# 时间线文件：S2 若在 workspace/memory/ 下定了正式时间线，改这个常量（或设环境变量 GUANJIA_TIMELINE）即可切过去
TIMELINE_PATH = Path(os.environ.get("GUANJIA_TIMELINE", DATA_DIR / "timeline.jsonl"))
HEARTBEAT_PATH = DATA_DIR / "last-seen.txt"
GAMES_PATH = REPO / "games.json"  # 进程白名单，仓库根目录，手工可编辑

OFFLINE, IDLE, ACTIVE, GAMING = "OFFLINE", "IDLE", "ACTIVE", "GAMING"

POLL_SEC = 2.0           # 检测周期
EXIT_HOLD_SEC = 60.0     # 退出 GAMING 的滞后：信号连续消失这么久才算打完
FULLSCREEN_HOLD_SEC = 10.0  # 全屏 + 高 GPU 需持续这么久才算游戏
GPU_BUSY_PCT = 60        # GPU 利用率阈值
QUIET_SEC = 300.0        # 开机 / 游戏结束后的静默期
HEARTBEAT_SEC = 30.0     # 心跳写盘周期
MIN_OFFLINE_SEC = 60.0   # 短于此的离线区间不写（例如守护进程秒级重启）
MANUAL_GAMING_TIMEOUT_S = 3 * 3600.0  # 手动"我打游戏了"最长维持时长，超时自动回落正常判定


def write_offline_interval(start: float, end: float, reason: str, path: Path | None = None) -> None:
    """往时间线追加一条离线区间。

    Args:
        start: 区间开始（epoch 秒）。
        end: 区间结束（epoch 秒）。
        reason: 原因，如"未运行""游戏"。
        path: 时间线文件，默认 TIMELINE_PATH（自测时注入临时文件）。
    一行一条 JSON：ts（区间开始，ISO 本地时间）、kind、text、source，另附 end/reason 便于机读。
    """
    path = path or TIMELINE_PATH
    s, e = datetime.fromtimestamp(start), datetime.fromtimestamp(end)
    fmt = "%H:%M" if s.date() == e.date() else "%m-%d %H:%M"
    rec = {
        "ts": s.isoformat(timespec="seconds"),
        "kind": "offline",
        "text": f"离线 从 {s.strftime(fmt)} 到 {e.strftime(fmt)}（{reason}）",
        "source": "guanjia.runtime",
        "end": e.isoformat(timespec="seconds"),
        "reason": reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.info("timeline: %s", rec["text"])


# ---------- Windows 探测 ----------
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def process_names() -> set[str]:
    """当前所有进程的可执行文件名（小写），用 Toolhelp32 快照，不引入 psutil。"""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    names: set[str] = set()
    e = _PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    try:
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            names.add(e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return names


def foreground_fullscreen() -> bool:
    """前台窗口是否铺满其所在显示器（独占全屏或无边框全屏）；桌面/任务栏本身不算。"""
    u32 = ctypes.windll.user32
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.MonitorFromWindow.restype = wintypes.HANDLE
    hwnd = u32.GetForegroundWindow()
    if not hwnd or hwnd == u32.GetShellWindow():
        return False
    cls = ctypes.create_unicode_buffer(64)
    u32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    r = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if not u32.GetMonitorInfoW(u32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):  # MONITOR_DEFAULTTONEAREST
        return False
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


def gpu_util() -> int:
    """nvidia-smi 查 GPU 利用率（多卡取最大）；查不到返回 0。"""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=0x08000000).stdout  # 不弹黑窗
        return max(int(x) for x in out.split())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def load_games(path: Path = GAMES_PATH) -> set[str]:
    """读进程白名单 games.json（{"processes": ["xxx.exe", ...]}），名字统一小写；文件缺失返回空集。"""
    try:
        return {p.lower() for p in json.loads(path.read_text(encoding="utf-8")).get("processes", [])}
    except (OSError, ValueError):
        return set()


# ---------- 状态机 ----------
class RuntimeMonitor:
    """运行态监控：后台线程轮询游戏信号、写心跳、推进 IDLE/ACTIVE/GAMING，状态变化回调 on_change(old, new)。

    探测函数与时钟都可注入，自测用假数据驱动；时钟是 epoch 秒（离线区间要落真实时间）。
    """

    def __init__(self, on_change: Callable[[str, str], None] | None = None, *,
                 clock: Callable[[], float] = time.time,
                 procs: Callable[[], set[str]] = process_names,
                 fullscreen: Callable[[], bool] = foreground_fullscreen,
                 gpu: Callable[[], int] = gpu_util,
                 games: Callable[[], set[str]] = load_games,
                 timeline: Path | None = None, heartbeat: Path = HEARTBEAT_PATH) -> None:
        self.on_change = on_change
        self.clock, self._procs, self._fullscreen, self._gpu, self._games = clock, procs, fullscreen, gpu, games
        self.timeline, self.heartbeat = timeline, heartbeat
        self.state = OFFLINE
        self.quiet_until = 0.0  # 主动侧在此时间戳（epoch 秒）之前不开口；S3 通过 is_quiet() 查询
        self.manual = False  # 手动"我打游戏了"
        self.reason = ""  # 最近一次判为游戏的依据，便于日志排查
        self._fs_since: float | None = None  # 全屏 + 高 GPU 开始持续的时刻
        self._last_seen_game: float | None = None  # 最近一次还检测到"在玩"的时刻（算滞后）
        self._manual_since: float | None = None  # 手动态开启的时刻，用于判断是否超过 MANUAL_GAMING_TIMEOUT_S
        self._manual_proc_seen = False  # 本次手动态期间是否曾检测到白名单进程（用于判断"有→无"）
        self._proc_hit = False  # 最近一次 _detect() 是否命中白名单进程
        self._game_start = 0.0
        self._last_beat = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- 对外接口 -----
    def start(self) -> None:
        """开机入口：补写离线区间、进入静默，再启动后台线程。"""
        self._boot()
        self._thread = threading.Thread(target=self._loop, name="runtime", daemon=True)
        self._thread.start()

    def _boot(self) -> None:
        """补写"上次心跳 → 现在"的离线区间，进入 IDLE 并静默 5 分钟，立刻写一次心跳。"""
        now = self.clock()
        self._recover_offline(now)
        self._set(IDLE)
        self.quiet_until = now + QUIET_SEC
        self._beat(now)

    def stop(self) -> None:
        """正常退出：最后写一次心跳后停线程（下次开机从这次心跳算离线）。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=POLL_SEC * 2)
        self._beat(self.clock())
        self._set(OFFLINE)

    def set_manual_gaming(self, on: bool) -> None:
        """手动开关："我打游戏了" 立即进 GAMING；"不打了" 立即退出（不等 60s 滞后，除非其它信号仍在）。

        手动开启还会记下开启时刻与"是否见过白名单进程"，供 tick() 里两条非语音退出口
        （超时 / 进程有→无）判断是否该自动关闭手动标记。
        """
        with self._lock:
            self.manual = on
            if on:
                self._manual_since = self.clock()
                self._manual_proc_seen = False
            else:
                self._last_seen_game = None
                self._manual_since = None
        self.tick()

    def is_quiet(self) -> bool:
        """主动侧查询：游戏中或静默期内都不许主动开口。"""
        return self.state != ACTIVE

    # ----- 核心：一次检测 -----
    def tick(self) -> None:
        """做一次检测并推进状态；后台线程每 POLL_SEC 调一次，自测直接手调。"""
        with self._lock:
            now = self.clock()
            # 非语音退出口 1：手动态超过 MANUAL_GAMING_TIMEOUT_S 自动关闭手动标记，回落正常信号判定
            if self.manual and self._manual_since is not None and now - self._manual_since >= MANUAL_GAMING_TIMEOUT_S:
                self.manual = False
                self._manual_since = None
            playing = self._detect(now)
            # 非语音退出口 2：手动态期间白名单进程"有→无"，视为用户已确实退出游戏，关闭手动标记
            # （不立即退出 GAMING，交给下面已有的 60s 滞后逻辑处理）
            if self.manual:
                if self._proc_hit:
                    self._manual_proc_seen = True
                elif self._manual_proc_seen:
                    self.manual = False
                    self._manual_since = None
            if playing:
                self._last_seen_game = now
            if self.state == GAMING:
                if not playing and (self._last_seen_game is None or now - self._last_seen_game >= EXIT_HOLD_SEC):
                    end = self._last_seen_game or now  # 离线区间结束于最后一次还在玩的时刻
                    self._set(IDLE)
                    self.quiet_until = now + QUIET_SEC
                    self._write_interval(self._game_start, end, "游戏")
            elif playing:
                self._game_start = now
                self._set(GAMING)
            elif self.state == IDLE and now >= self.quiet_until:
                self._set(ACTIVE)
            if now - self._last_beat >= HEARTBEAT_SEC:
                self._beat(now)

    def _detect(self, now: float) -> bool:
        """按优先级判断此刻是否"在玩"：进程白名单 > 全屏+GPU 持续 10s > 手动。"""
        games = self._games()
        hit = games & self._procs() if games else set()
        self._proc_hit = bool(hit)  # 记下本次是否命中白名单，供手动态"进程有→无"判断使用
        if hit:
            self.reason = f"process:{','.join(sorted(hit))}"
            return True
        # 全屏才去查 GPU（nvidia-smi 有开销），不全屏立即清计时
        if self._fullscreen() and self._gpu() > GPU_BUSY_PCT:
            self._fs_since = self._fs_since or now
            if now - self._fs_since >= FULLSCREEN_HOLD_SEC:
                self.reason = "fullscreen+gpu"
                return True
        else:
            self._fs_since = None
        if self.manual:
            self.reason = "manual"
            return True
        return False

    # ----- 内部 -----
    def _loop(self) -> None:
        while not self._stop.wait(POLL_SEC):
            try:
                self.tick()
            except Exception:  # noqa: BLE001  探测偶发失败不能让监控线程退出
                log.exception("runtime tick error")

    def _set(self, new: str) -> None:
        old, self.state = self.state, new
        if old == new:
            return
        log.info("runtime %s -> %s%s", old, new, f"（{self.reason}）" if new == GAMING else "")
        if self.on_change:
            try:
                self.on_change(old, new)
            except Exception:  # noqa: BLE001
                log.exception("runtime on_change error")

    def _beat(self, now: float) -> None:
        """写心跳：时间戳 + 当前状态；GAMING 时带上游戏开始时刻，关机时正在玩也能补回游戏区间。"""
        self._last_beat = now
        rec = {"ts": now, "state": self.state, "game_start": self._game_start if self.state == GAMING else None}
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.heartbeat.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, self.heartbeat)  # 原子替换，断电也不会留半截文件

    def _recover_offline(self, now: float) -> None:
        """开机补写：读上次心跳；上次在玩则先补"游戏"区间，再补"上次心跳 → 现在"的"未运行"区间。"""
        try:
            last = json.loads(self.heartbeat.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # 首次运行或心跳损坏：无从推断，不写
        seen = float(last["ts"])
        if last.get("state") == GAMING and last.get("game_start"):
            self._write_interval(float(last["game_start"]), seen, "游戏")
        self._write_interval(seen, now, "未运行")

    def _write_interval(self, start: float, end: float, reason: str) -> None:
        if end - start >= MIN_OFFLINE_SEC:
            write_offline_interval(start, end, reason, self.timeline)


# ---------- 自测 ----------
def _selftest() -> None:
    """不接音频：假时钟 + 假探测走一遍 开机补写 → 静默 → ACTIVE → 进程/全屏/手动进出 GAMING → 60s 滞后。"""
    tmp = Path(tempfile.mkdtemp(prefix="guanjia_rt_"))
    tl, hb = tmp / "timeline.jsonl", tmp / "last-seen.txt"
    t = [1_790_000_000.0]
    fake = {"procs": set(), "fs": False, "gpu": 0}
    changes: list[tuple[str, str]] = []
    hb.write_text(json.dumps({"ts": t[0] - 3600, "state": ACTIVE, "game_start": None}))  # 一小时前"关机"

    m = RuntimeMonitor(lambda o, n: changes.append((o, n)), clock=lambda: t[0],
                       procs=lambda: fake["procs"], fullscreen=lambda: fake["fs"], gpu=lambda: fake["gpu"],
                       games=lambda: {"league of legends.exe"}, timeline=tl, heartbeat=hb)
    m._boot()  # 等同 start() 但不起后台线程，下面手动 tick 推进假时钟

    def step(sec: float, n: int = 1) -> None:
        for _ in range(n):
            t[0] += sec
            m.tick()

    def check(cond: bool, what: str) -> None:
        print(("PASS " if cond else "FAIL ") + what)
        if not cond:
            raise SystemExit(1)

    check(m.state == IDLE and m.is_quiet(), "开机进入 IDLE 静默")
    check("未运行" in tl.read_text(encoding="utf-8"), "开机补写 未运行 离线区间")
    step(POLL_SEC, int(QUIET_SEC // POLL_SEC) - 1)
    check(m.state == IDLE, "静默 5 分钟内保持 IDLE")
    step(POLL_SEC, 1)
    check(m.state == ACTIVE and not m.is_quiet(), "静默期满进入 ACTIVE")

    # 进程白名单：LeagueClient 常驻不算，真正对局进程才算
    fake["procs"] = {"leagueclient.exe"}
    step(POLL_SEC)
    check(m.state == ACTIVE, "只有 LeagueClient.exe 不判游戏")
    fake["procs"] = {"leagueclient.exe", "league of legends.exe"}
    step(POLL_SEC)
    check(m.state == GAMING and m.reason.startswith("process"), "对局进程出现 → GAMING")
    step(POLL_SEC, 900)  # 玩 30 分钟
    fake["procs"] = {"leagueclient.exe"}
    step(POLL_SEC, int(EXIT_HOLD_SEC // POLL_SEC) - 1)
    check(m.state == GAMING, "信号消失未满 60s 仍 GAMING（滞后）")
    fake["procs"] = {"leagueclient.exe", "league of legends.exe"}  # 中途回来（读条/重连）
    step(POLL_SEC)
    fake["procs"] = {"leagueclient.exe"}
    step(POLL_SEC, int(EXIT_HOLD_SEC // POLL_SEC) - 1)
    check(m.state == GAMING, "信号闪回后滞后重新计时")
    step(POLL_SEC, 1)
    check(m.state == IDLE and m.quiet_until > t[0], "信号连续消失 60s → IDLE 并重新静默 5 分钟")
    lines = tl.read_text(encoding="utf-8").strip().splitlines()
    check(json.loads(lines[-1])["reason"] == "游戏", "游戏时段补写为离线区间")

    # 全屏 + GPU：需持续 10s；GPU 不够或不全屏都不算
    step(POLL_SEC, int(QUIET_SEC // POLL_SEC))
    check(m.state == ACTIVE, "静默期满回到 ACTIVE")
    fake["fs"], fake["gpu"] = True, 40
    step(POLL_SEC, 10)
    check(m.state == ACTIVE, "全屏但 GPU 40% 不判游戏")
    fake["gpu"] = 85
    step(POLL_SEC, int(FULLSCREEN_HOLD_SEC // POLL_SEC))  # 首次观测起算，第 6 次 tick 才满 10s
    check(m.state == ACTIVE, "全屏+GPU 85% 未满 10s 不判游戏")
    step(POLL_SEC, 1)
    check(m.state == GAMING and m.reason == "fullscreen+gpu", "全屏+GPU 持续 10s → GAMING")
    fake["fs"] = False
    step(POLL_SEC, int(EXIT_HOLD_SEC // POLL_SEC))
    check(m.state == IDLE, "退出全屏 60s 后离开 GAMING")

    # 手动开关：开立即进，关立即出
    step(POLL_SEC, int(QUIET_SEC // POLL_SEC))
    m.set_manual_gaming(True)
    check(m.state == GAMING and m.reason == "manual", "手动 我打游戏了 → 立即 GAMING")
    step(POLL_SEC, 30)
    m.set_manual_gaming(False)
    check(m.state == IDLE, "手动关闭 → 立即离开 GAMING")

    # 非语音退出口 1：手动态超时（默认 3 小时）自动关闭手动标记，回落正常判定
    step(POLL_SEC, int(QUIET_SEC // POLL_SEC))
    check(m.state == ACTIVE, "静默期满回到 ACTIVE（备超时用例）")
    m.set_manual_gaming(True)
    check(m.state == GAMING and m.manual, "手动开启 → GAMING")
    step(POLL_SEC, int(MANUAL_GAMING_TIMEOUT_S // POLL_SEC))
    check(not m.manual, "手动态超过 MANUAL_GAMING_TIMEOUT_S → 自动关闭手动标记")
    step(POLL_SEC, int(EXIT_HOLD_SEC // POLL_SEC) + 1)
    check(m.state == IDLE, "手动标记关闭后无其它信号，滞后期满退出 GAMING")

    # 非语音退出口 2：手动态期间白名单进程"有→无"，视为用户已退出游戏，自动关闭手动标记（仍走 60s 滞后）
    step(POLL_SEC, int(QUIET_SEC // POLL_SEC))
    check(m.state == ACTIVE, "静默期满回到 ACTIVE（备进程消失用例）")
    fake["procs"] = set()
    m.set_manual_gaming(True)
    check(m.state == GAMING and m.manual, "手动开启（尚无白名单进程）→ GAMING")
    fake["procs"] = {"leagueclient.exe", "league of legends.exe"}
    step(POLL_SEC)
    check(m.manual, "白名单进程出现，手动标记仍保留")
    fake["procs"] = set()
    step(POLL_SEC)
    check(not m.manual, "白名单进程由有变无 → 自动关闭手动标记")
    check(m.state == GAMING, "关闭手动标记后仍在 60s 滞后期内，未立即退出")
    step(POLL_SEC, int(EXIT_HOLD_SEC // POLL_SEC))
    check(m.state == IDLE, "滞后期满 → 退出 GAMING")
    step(POLL_SEC, int(QUIET_SEC // POLL_SEC))
    check(m.state == ACTIVE, "静默期满回到 ACTIVE")

    # 关机时正在玩：心跳带 game_start，下次开机补两条
    m.set_manual_gaming(True)
    step(HEARTBEAT_SEC, 10)
    beat = json.loads(hb.read_text())
    check(beat["state"] == GAMING and beat["game_start"], "游戏中心跳记录 game_start")
    t[0] += 7200
    n0 = len(tl.read_text(encoding="utf-8").splitlines())
    m2 = RuntimeMonitor(clock=lambda: t[0], procs=set, fullscreen=lambda: False, gpu=lambda: 0,
                        games=set, timeline=tl, heartbeat=hb)
    m2._recover_offline(t[0])
    new = [json.loads(x)["reason"] for x in tl.read_text(encoding="utf-8").splitlines()[n0:]]
    check(new == ["游戏", "未运行"], "游戏中关机 → 开机补写 游戏 + 未运行 两条")

    print("transitions:", " ".join(f"{o}->{n}" for o, n in changes))
    print("timeline sample:", tl.read_text(encoding="utf-8").splitlines()[0])
    print("ALL PASS")


def _probe() -> None:
    """在真机上打印一次真实探测结果（不改状态、不写文件）。"""
    games = load_games()
    print("games.json:", sorted(games))
    print("process hit:", sorted(games & process_names()))
    print("foreground fullscreen:", foreground_fullscreen())
    print("gpu util:", gpu_util())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="假时钟/假探测走一遍状态迁移")
    ap.add_argument("--probe", action="store_true", help="打印一次真实探测结果")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.probe:
        _probe()
    else:
        _selftest()
