"""Q0 前台程序元数据和影音候选判定：全用 fixture，不访问真实窗口。"""

import queue
import threading

from guanjia.program_context import ForegroundApp, classify_media, snapshot
from guanjia.settings import ProgramQuietSettings, SettingsStore, from_dict, to_dict


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


config = from_dict({
    "program_quiet": {
        "enabled": True,
        "media_apps": ["PotPlayerMini64.exe"],
        "browser_apps": ["msedge.exe"],
        "browser_title_keywords": ["哔哩哔哩", "直播"],
    },
}).program_quiet
check(config.enabled and config.media_apps == ("PotPlayerMini64.exe",), "program quiet config persists as model")
check(to_dict(from_dict({"program_quiet": {"browser_apps": ["chrome.exe"]}}))["program_quiet"]["browser_apps"] == ["chrome.exe"],
      "program quiet config serializes as JSON lists")

player = ForegroundApp(pid=10, exe_name="potplayermini64.exe", title="节目", visible=True, fullscreen=False)
check(classify_media(player, config).rule == "media_app", "foreground media app is candidate")
browser = ForegroundApp(pid=11, exe_name="msedge.exe", title="哔哩哔哩 直播", visible=True, fullscreen=True)
check(classify_media(browser, config).rule == "browser_fullscreen_title", "fullscreen configured browser title is candidate")
check(classify_media(ForegroundApp(exe_name="msedge.exe", title="文档", visible=True, fullscreen=True), config).rule == "no_match",
      "fullscreen browser without video title is not candidate")
check(classify_media(ForegroundApp(error="win32:OSError"), config).rule == "probe_failed",
      "probe failure fails open")
check(classify_media(player, ProgramQuietSettings()).rule == "disabled", "default config changes no behavior")

view = snapshot(ForegroundApp(pid=12, exe_name="tvbox.exe", exe_path=r"C:\secret\tvbox.exe", title="x" * 120, visible=True), classify_media(player, config))
check("exe_path" not in view and len(view["title"]) == 80 and "media_candidate" in view, "status snapshot redacts path and bounds title")

# status 只读暴露前台候选，不需真实 Windows 窗口。
import guanjia.voice as voice

original_foreground = voice.foreground_app
voice.foreground_app = lambda: ForegroundApp(pid=99, exe_name="potplayermini64.exe", title="节目", visible=True)
try:
    daemon = object.__new__(voice.VoiceDaemon)
    daemon.view = lambda: type("View", (), {"key": "listening", "label": "在听"})()
    daemon._muted, daemon._paused = threading.Event(), threading.Event()
    daemon.runtime = type("Runtime", (), {"state": "ACTIVE"})()
    daemon.health = type("Health", (), {"gateway": True, "tts": True})()
    daemon.last_error, daemon.sleep_state = "", "awake"
    daemon.text_q, daemon._followup_until, daemon.guard = queue.Queue(), 0.0, None
    daemon.store, daemon._last_program_snapshot = SettingsStore(None), None
    status = daemon.status()
    check(status["program"]["exe_name"] == "potplayermini64.exe" and status["program"]["media_candidate"] is False,
          "voice status exposes observation without enabling quiet")
finally:
    voice.foreground_app = original_foreground
print("ALL PASS test_program_context")
