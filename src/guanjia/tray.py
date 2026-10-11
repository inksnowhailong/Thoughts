"""系统托盘图标（pystray）：颜色表示状态，左键 = 切换静音，右键菜单 = 状态 / 打开面板 / 静音 / 重启语音 / 退出语音。

托盘跑在自己的线程里；菜单回调只调守护进程的线程安全方法（toggle_mute / request_exit）。
平时不弹通知；只有 notify() 会弹气泡（目前仅「管家在等你确认」，且游戏中由守护进程决定不调用）。
pystray 的 Windows 后端在构造 Icon 时就建隐藏窗口，窗口消息只投给建它的线程，所以 Icon 必须在托盘线程里构造并 run。

Windows 11 默认把新托盘图标收进「^」。系统在 HKCU\\Control Panel\\NotifyIconSettings 下给每个图标建一个子键，
值 IsPromoted=1 表示固定显示在任务栏上（未公开但广泛使用的做法）。图标出现几秒后我们找到提示文字以「管家」开头的那一项，
只在 IsPromoted 还不存在时（从没被人调过）设成 1；主人以后自己拖回「^」里，系统写成 0，我们不再动它。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from PIL import Image, ImageDraw

from guanjia.status import TrayView

log = logging.getLogger("guanjia.tray")
ICON_SIZE = 64
NOTIFY_SETTINGS_KEY = r"Control Panel\NotifyIconSettings"
TOOLTIP_PREFIX = "管家"


def make_icon_image(color: tuple[int, int, int], size: int = ICON_SIZE) -> Image.Image:
    """实心圆 + 白色细环，四角透明：托盘里一眼能分出颜色。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 16
    d.ellipse((m, m, size - 1 - m, size - 1 - m), fill=color + (255,), outline=(255, 255, 255, 255),
              width=max(2, size // 16))
    return img


class WinregNotifySettings:
    """NotifyIconSettings 的读写（只碰当前用户的 HKCU）。没有这个键（Windows 10）时 entries() 返回空。"""

    def entries(self) -> list[tuple[str, dict]]:
        import winreg

        out = []
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_SETTINGS_KEY)
        except OSError:
            return out
        with root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                vals = {}
                try:
                    with winreg.OpenKey(root, name) as k:
                        j = 0
                        while True:
                            try:
                                vname, vdata, _ = winreg.EnumValue(k, j)
                            except OSError:
                                break
                            j += 1
                            if isinstance(vdata, (str, int)):
                                vals[vname] = vdata
                except OSError:
                    continue
                out.append((name, vals))
        return out

    def promote(self, name: str) -> None:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_SETTINGS_KEY + "\\" + name, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD, 1)


def promote_tray_icon(reg=None, prefix: str = TOOLTIP_PREFIX) -> list[str]:
    """把提示文字以「管家」开头、且从没设过 IsPromoted 的托盘项设为固定显示。返回改了哪些子键。"""
    reg = reg or WinregNotifySettings()
    changed = []
    try:
        for name, vals in reg.entries():
            if str(vals.get("InitialTooltip", "")).startswith(prefix) and "IsPromoted" not in vals:
                reg.promote(name)
                changed.append(name)
    except OSError as e:
        log.warning("tray promote failed: %s", e)
    if changed:
        log.info("tray icon promoted to taskbar: %s", changed)
    return changed


class Tray:
    def __init__(self, view: Callable[[], TrayView], muted: Callable[[], bool], on_toggle_mute: Callable[[], None],
                 on_restart: Callable[[], None], on_exit: Callable[[], None],
                 on_open_panel: Callable[[], None] | None = None) -> None:
        self.view = view
        self.muted = muted
        self.on_toggle_mute = on_toggle_mute
        self.on_restart = on_restart
        self.on_exit = on_exit
        self.on_open_panel = on_open_panel
        self._icon = None
        self._shown_key = ""
        self._lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, name="tray", daemon=True).start()
        threading.Thread(target=self._promote_later, name="tray-promote", daemon=True).start()

    def _promote_later(self) -> None:
        """图标出现后系统才建注册表项：5 秒、60 秒各试一次。"""
        for delay in (5, 55):
            time.sleep(delay)
            if promote_tray_icon():
                return

    def menu_items(self, pystray) -> list:
        """右键菜单（单独成函数便于单测检查有哪些项）。"""
        items = [pystray.MenuItem(lambda item: self.view().label, None, enabled=False)]
        if self.on_open_panel is not None:
            items.append(pystray.MenuItem("打开面板（控制与调试）", lambda icon, item: self.on_open_panel()))
        items += [
            pystray.MenuItem("静音", lambda icon, item: self.on_toggle_mute(), checked=lambda item: self.muted(),
                             default=True),  # default = 左键单击触发
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重启语音", lambda icon, item: self.on_restart()),
            pystray.MenuItem("退出语音", lambda icon, item: self.on_exit()),
        ]
        return items

    def _run(self) -> None:
        """托盘线程：构造 Icon 并跑它的消息循环（两者必须同一线程）。"""
        try:
            import pystray  # 延迟导入：单测和 --no-tray 不需要它

            v = self.view()
            self._shown_key = v.key
            self._icon = pystray.Icon("guanjia", make_icon_image(v.color), v.tooltip, pystray.Menu(*self.menu_items(pystray)))
            self._icon.run()
        except Exception:  # noqa: BLE001  托盘挂了不影响听说
            log.exception("tray stopped with error")

    def refresh(self) -> None:
        """状态变化后调用：换图标颜色、提示文字，刷新菜单勾选。"""
        if not self._icon:
            return
        with self._lock:
            v = self.view()
            try:
                if v.key != self._shown_key:
                    self._icon.icon = make_icon_image(v.color)
                    self._shown_key = v.key
                self._icon.title = v.tooltip
                self._icon.update_menu()
            except Exception:  # noqa: BLE001
                log.exception("tray refresh failed")

    def notify(self, message: str, title: str = TOOLTIP_PREFIX) -> bool:
        """弹一条托盘气泡（Windows 通知）。图标还没建好、或系统拒绝返回 False。调用方负责「游戏中不弹」。
        Windows 气泡标题上限 63、正文上限 255 个字符，超了会弹不出来，这里截断。"""
        if not self._icon:
            return False
        try:
            self._icon.notify(message[:250], title[:60])
            return True
        except Exception:  # noqa: BLE001  气泡弹不出来不影响图标变色
            log.exception("tray notify failed")
            return False

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
