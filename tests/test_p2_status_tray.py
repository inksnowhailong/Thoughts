"""P2 健康检查 / 托盘状态 / 托盘图标用例（不启动托盘消息循环，不需要桌面）。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p2_status_tray.py
"""
import socket

from guanjia.status import COLORS, Health, HealthMonitor, tcp_alive, tray_view
from guanjia.tray import ICON_SIZE, Tray, make_icon_image


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


ok = Health(True, True)
v = tray_view(muted=False, gaming=False, busy=False, health=ok)
check(v.key == "listening" and v.label == "在听" and "网关✓ TTS✓" in v.tooltip, "healthy -> listening")
check(v.tooltip.startswith("思绪：") and "管家" not in v.tooltip, "tooltip shows the display name 思绪")
check(tray_view(muted=False, gaming=False, busy=True, health=ok).key == "busy", "speaking -> busy")
check(tray_view(muted=True, gaming=False, busy=False, health=ok).key == "muted", "muted")
check(tray_view(muted=False, gaming=True, busy=False, health=ok).key == "gaming", "gaming")
check(tray_view(muted=True, gaming=True, busy=False, health=ok).key == "muted", "muted wins over gaming")
e1 = tray_view(muted=True, gaming=False, busy=False, health=Health(False, True))
check(e1.key == "error" and "网关" in e1.label, "gateway offline is an error (wins over muted)")
check(tray_view(muted=False, gaming=False, busy=False, health=Health(True, False)).label == "出错：TTS 不在线", "tts offline")
check(tray_view(muted=False, gaming=False, busy=False, health=ok, last_error="TTS：ReadTimeout").key == "error", "last turn error")
check(tray_view(muted=False, gaming=False, busy=False, health=Health()).key == "starting", "unknown health -> starting")
check("休眠中" in tray_view(muted=False, gaming=False, busy=False, health=ok, sleep_state="sleeping").tooltip, "P4 sleep bit in tooltip")
check(len(tray_view(muted=False, gaming=False, busy=False, health=ok, last_error="x" * 300).tooltip) <= 120, "tooltip <= 120 chars")
check(set(COLORS) == {"listening", "busy", "muted", "gaming", "error", "starting", "pending"}, "every state has a color")

calls: list[Health] = []
vals = {"gateway": True, "tts": True}
m = HealthMonitor({"gateway": lambda: vals["gateway"], "tts": lambda: vals["tts"]}, on_change=calls.append, interval=60)
m.check_once()
m.check_once()
check(len(calls) == 1 and calls[0] == Health(True, True), "no repeated callback when unchanged")
vals["tts"] = False
m.check_once()
check(len(calls) == 2 and calls[1] == Health(True, False), "callback on change")


def boom() -> bool:
    raise OSError("x")


m2 = HealthMonitor({"gateway": boom, "tts": lambda: True}, on_change=calls.append)
check(m2.check_once() == Health(False, True), "a raising check counts as offline")

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
check(tcp_alive("127.0.0.1", port), "listening port -> alive")
srv.close()
check(not tcp_alive("127.0.0.1", port, timeout=0.5), "closed port -> not alive")

img = make_icon_image(COLORS["error"])
check(img.size == (ICON_SIZE, ICON_SIZE) and img.mode == "RGBA", "icon is 64x64 RGBA")
check(img.getpixel((32, 32)) == COLORS["error"] + (255,), "icon center has the state color")
check(img.getpixel((0, 0))[3] == 0, "icon corners transparent")
t = Tray(view=lambda: v, muted=lambda: False, on_toggle_mute=lambda: None, on_restart=lambda: None, on_exit=lambda: None)
t.refresh()
t.stop()
check(True, "refresh/stop on a tray that was never started do nothing")
print("ALL PASS test_p2_status_tray")
