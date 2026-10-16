"""前台窗口元数据与影音候选判定；只读、失败时不改变语音行为。"""

from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ForegroundApp:
    """前台窗口的最小可观测元数据；error 表示探测降级，不是静默依据。"""

    pid: int | None = None
    exe_name: str = ""
    exe_path: str = ""
    title: str = ""
    visible: bool = False
    minimized: bool = False
    fullscreen: bool = False
    error: str = ""


@dataclass(frozen=True)
class ProgramJudgement:
    """只读判定，Q0 不据此改变播放或唤醒。"""

    candidate: bool
    rule: str


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _fullscreen(hwnd, user32) -> bool:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    bounds = info.rcMonitor
    return rect.left <= bounds.left and rect.top <= bounds.top and rect.right >= bounds.right and rect.bottom >= bounds.bottom


def foreground_app() -> ForegroundApp:
    """读取当前前台窗口；任何 Win32/权限问题都 fail-open 返回 error。"""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd == user32.GetShellWindow():
            return ForegroundApp(error="no_foreground_window")
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, len(title))
        visible, minimized = bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd))
        path = ""
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    path = buf.value
            finally:
                kernel32.CloseHandle(handle)
        return ForegroundApp(
            pid=pid.value or None,
            exe_name=Path(path).name.casefold() if path else "",
            exe_path=path,
            title=title.value,
            visible=visible,
            minimized=minimized,
            fullscreen=visible and not minimized and _fullscreen(hwnd, user32),
            error="" if path else "process_path_unavailable",
        )
    except Exception as exc:  # noqa: BLE001  探测绝不能影响语音主循环
        return ForegroundApp(error=f"win32:{type(exc).__name__}")


def classify_media(app: ForegroundApp, config) -> ProgramJudgement:
    """按配置判断影音候选；不读取音频会话、不枚举后台应用。"""
    if not getattr(config, "enabled", False):
        return ProgramJudgement(False, "disabled")
    if app.error and not app.exe_name:
        return ProgramJudgement(False, "probe_failed")
    exe = app.exe_name.casefold()
    media = {str(value).casefold() for value in getattr(config, "media_apps", ())}
    if exe and exe in media:
        return ProgramJudgement(True, "media_app")
    browsers = {str(value).casefold() for value in getattr(config, "browser_apps", ())}
    keywords = tuple(str(value).casefold() for value in getattr(config, "browser_title_keywords", ()))
    if exe in browsers and app.fullscreen and any(word in app.title.casefold() for word in keywords):
        return ProgramJudgement(True, "browser_fullscreen_title")
    return ProgramJudgement(False, "no_match")


def snapshot(app: ForegroundApp, judgement: ProgramJudgement) -> dict:
    """面板/状态安全摘要：完整路径不外露，标题截断。"""
    data = asdict(app)
    data.pop("exe_path", None)
    data["title"] = app.title[:80]
    data["media_candidate"] = judgement.candidate
    data["media_rule"] = judgement.rule
    return data
