"""Q1 程序影音候选状态机和面板设置：全用 fixture，不读真实窗口。"""

from guanjia.events import EventHub
from guanjia.panel import PANEL_HTML, PanelBackend
from guanjia.program_context import ForegroundApp
from guanjia.program_quiet import CANDIDATE_ENTER, CANDIDATE_EXIT, NORMAL, QUIET_ACTIVE, ProgramQuietMonitor
from guanjia.settings import ProgramQuietSettings, Settings, SettingsStore


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


now = [0.0]
app = [ForegroundApp(exe_name="potplayer.exe", title="节目", visible=True)]
config = [ProgramQuietSettings(enabled=True, enter_hold_sec=5, exit_hold_sec=15, media_apps=("potplayer.exe",))]
changes = []
monitor = ProgramQuietMonitor(lambda: config[0], probe=lambda: app[0], clock=lambda: now[0],
                              on_change=lambda old, new, snap: changes.append((old, new, snap["media_rule"])))

monitor.tick()
check(monitor.state == CANDIDATE_ENTER and not monitor.quiet_active, "candidate requires enter hold")
now[0] = 4.9
monitor.tick()
check(monitor.state == CANDIDATE_ENTER, "short foreground switch does not enter")
now[0] = 5.0
monitor.tick()
check(monitor.state == QUIET_ACTIVE and monitor.quiet_active, "stable media foreground enters quiet candidate")

app[0] = ForegroundApp(exe_name="explorer.exe", title="文件", visible=True)
now[0] = 6.0
monitor.tick()
check(monitor.state == CANDIDATE_EXIT and monitor.quiet_active, "exit hold keeps quiet state stable")
now[0] = 20.9
monitor.tick()
check(monitor.state == CANDIDATE_EXIT, "exit hold prevents flicker")
now[0] = 21.0
monitor.tick()
check(monitor.state == NORMAL, "stable non-media foreground exits")

app[0] = ForegroundApp(error="win32:OSError")
now[0] = 22.0
monitor.tick()
check(monitor.state == NORMAL and monitor.snapshot()["media_rule"] == "probe_failed", "probe failure fails open")
check(any(new == QUIET_ACTIVE for _, new, _ in changes), "state transitions are observable")

store = SettingsStore(None)
backend = PanelBackend(EventHub(), store, status=lambda: {"program": monitor.snapshot()}, control=lambda _: (True, ""), inject=lambda *_: {})
ok, data = backend.update_settings({"program_quiet": {"enabled": True, "media_apps": ["potplayer.exe"], "enter_hold_sec": 6}})
check(ok and data["program_quiet"]["media_apps"] == ["potplayer.exe"], "panel accepts nested program quiet settings")
html = PANEL_HTML.read_text(encoding="utf-8")
check("s_pq_enabled" in html and "programDiag" in html and "program_quiet" in html, "panel displays config and diagnostics")
print("ALL PASS test_program_quiet")
