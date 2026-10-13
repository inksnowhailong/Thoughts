"""TTS 服务的内存 / 显存工具（只用标准库；显存读数、退出动作都由调用方注入，所以不加载模型也能测试）。

为什么有它（2026-10-13 事故）：主人反复开关游戏，语音每次进游戏态都让 TTS 进程内 /unload，退出时 /load。
进程内反复卸载 / 重载 7.5GB 峰值的模型，宿主内存被碎片化的堆撑到 9~14GB，WSL 16GB 被撑爆，TTS 被 OOM 杀三次（每次约 15 分钟没人拉起）。

新做法（卸载不在进程内做）：
  - /unload 只是「进游戏态」的信号：空闲显存够就什么都不动（短游戏、视频全屏根本不打扰 TTS）；
  - 显存真的不够（< keep_free_mb）才让进程退出（退出码 75），内存和显存一次性归零；
  - 退出游戏后由 scripts/wsl-tts-launcher.ps1 等显存空出来再重新拉起；/load 在服务活着时只是取消看守。
"""
from __future__ import annotations

import ctypes
import threading
from typing import Callable, Optional

EXIT_RELEASED = 75  # 为游戏腾显存主动退出（EX_TEMPFAIL）；启动器看到它就等显存空出来再拉起，不算崩溃


def rss_mb(status_path: str = "/proc/self/status") -> Optional[int]:
    """当前进程常驻内存（MB），读 VmRSS；读不到（非 Linux 等）返回 None。"""
    try:
        with open(status_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def trim_heap() -> bool:
    """把 glibc 堆里空闲的页还给系统（卸载后 RSS 不降，多半是这些碎片）；非 glibc 平台返回 False，不抛异常。"""
    try:
        return bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except (OSError, AttributeError):
        return False


class GamingGuard:
    """游戏态显存看守：/unload 时开始盯空闲显存，真不够才触发 on_release（服务端用它退出进程）。"""

    def __init__(
        self,
        free_mb: Callable[[], Optional[int]],
        on_release: Callable[[int, str], None],
        keep_free_mb: int = 3072,
        poll_sec: float = 3.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.free_mb = free_mb  # 返回全卡空闲显存 MB（含游戏占用）；读不到返回 None
        self.on_release = on_release  # (退出码, 原因)
        self.keep_free_mb = keep_free_mb  # 空闲显存低于它就释放；<=0 表示关闭本功能
        self.poll_sec = poll_sec
        self.log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._released = False

    @property
    def watching(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def enter(self) -> str:
        """/unload：进游戏态。返回 'off'（功能关闭）/ 'exit'（当场就不够，已触发释放）/ 'keep'（够，后台继续盯）。"""
        if self.keep_free_mb <= 0:
            return "off"
        with self._lock:
            if self._released:
                return "exit"
            if self.watching:
                return "keep"
            self._stop.clear()
        if self._check():
            return "exit"
        with self._lock:
            if not self.watching and not self._released:
                self._thread = threading.Thread(target=self._run, name="tts-gaming-watch", daemon=True)
                self._thread.start()
        return "keep"

    def leave(self) -> None:
        """/load：退出游戏态，取消看守（模型一直在内存里，不需要重载）。"""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_sec):
            if self._check():
                return

    def _check(self) -> bool:
        """读一次空闲显存；不够就触发释放并返回 True。读数失败当作「够」，绝不误杀。"""
        try:
            free = self.free_mb()
        except Exception as e:  # noqa: BLE001
            self.log(f"gaming guard: free VRAM probe failed: {e!r}")
            return False
        if free is None or free >= self.keep_free_mb:
            return False
        with self._lock:
            if self._released:
                return True
            self._released = True
        self._stop.set()
        reason = f"free VRAM {free}MB < {self.keep_free_mb}MB while gaming"
        self.log(f"gaming guard: {reason}, releasing (exit {EXIT_RELEASED})")
        self.on_release(EXIT_RELEASED, reason)
        return True
