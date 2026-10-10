"""系统托盘图标（pystray）：颜色表示状态，左键 = 切换静音，右键菜单 = 状态 / 静音 / 重启语音 / 退出语音。

托盘跑在自己的线程里；菜单回调只调守护进程的线程安全方法（toggle_mute / request_exit）。不弹任何通知。
pystray 的 Windows 后端在构造 Icon 时就建隐藏窗口，窗口消息只投给建它的线程，所以 Icon 必须在托盘线程里构造并 run。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PIL import Image, ImageDraw

from guanjia.status import TrayView

log = logging.getLogger("guanjia.tray")
ICON_SIZE = 64


def make_icon_image(color: tuple[int, int, int], size: int = ICON_SIZE) -> Image.Image:
    """实心圆 + 白色细环，四角透明：托盘里一眼能分出颜色。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 16
    d.ellipse((m, m, size - 1 - m, size - 1 - m), fill=color + (255,), outline=(255, 255, 255, 255),
              width=max(2, size // 16))
    return img


class Tray:
    def __init__(self, view: Callable[[], TrayView], muted: Callable[[], bool], on_toggle_mute: Callable[[], None],
                 on_restart: Callable[[], None], on_exit: Callable[[], None]) -> None:
        self.view = view
        self.muted = muted
        self.on_toggle_mute = on_toggle_mute
        self.on_restart = on_restart
        self.on_exit = on_exit
        self._icon = None
        self._shown_key = ""
        self._lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, name="tray", daemon=True).start()

    def _run(self) -> None:
        """托盘线程：构造 Icon 并跑它的消息循环（两者必须同一线程）。"""
        try:
            import pystray  # 延迟导入：单测和 --no-tray 不需要它

            v = self.view()
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: self.view().label, None, enabled=False),
                pystray.MenuItem("静音", lambda icon, item: self.on_toggle_mute(), checked=lambda item: self.muted(),
                                 default=True),  # default = 左键单击触发
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("重启语音", lambda icon, item: self.on_restart()),
                pystray.MenuItem("退出语音", lambda icon, item: self.on_exit()),
            )
            self._shown_key = v.key
            self._icon = pystray.Icon("guanjia", make_icon_image(v.color), v.tooltip, menu)
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

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
