"""「管家面板」的后端：把守护进程的状态、每句话的记录、事件流、控制和设置接到控制口上。

页面是同目录的 panel.html（单个静态文件，不引外部资源，离线可用）。每次守护进程启动随机生成一个令牌，
只写进 /panel 返回的页面里；控制口要求 /api/* 带上它（见 control.py 的说明）。
"""

from __future__ import annotations

import queue
import secrets
from collections.abc import Callable
from pathlib import Path

from guanjia.events import LOG_MODULES, STAGE_CN, STAGES, EventHub
from guanjia.params import describe
from guanjia.prompts import PromptService
from guanjia.settings import SettingsStore, to_dict

PANEL_HTML = Path(__file__).with_name("panel.html")
PANEL_URL = "http://127.0.0.1:18795/panel"
CONTROL_ACTIONS = ("mute", "unmute", "restart", "quit")
DEFAULT_HIDDEN_MODULES = ("health", "barge-mon")  # 面板日志默认不显示的高频行


class PanelBackend:
    def __init__(self, hub: EventHub, store: SettingsStore, *, status: Callable[[], dict],
                 control: Callable[[str], tuple[bool, str]], inject: Callable[[str, bool], dict],
                 html_path: Path = PANEL_HTML, token: str | None = None) -> None:
        self.hub = hub
        self.store = store
        self._status = status
        self._control = control
        self._inject = inject
        self.html_path = html_path
        self.token = token or secrets.token_urlsafe(24)
        # 通用路由表：功能模块往里注册 /api/xxx，control.py 不用再改。路由函数返回 (状态码, JSON)，抛 ValueError 回 400
        self._get: dict[str, Callable[[dict], tuple[int, dict]]] = {}
        self._post: dict[str, Callable[[dict], tuple[int, dict]]] = {}

    def register_get(self, path: str, fn: Callable[[dict], tuple[int, dict]]) -> None:
        self._get[path] = fn

    def register_post(self, path: str, fn: Callable[[dict], tuple[int, dict]]) -> None:
        self._post[path] = fn

    @staticmethod
    def _call(fn: Callable[[dict], tuple[int, dict]], arg: dict) -> tuple[int, dict]:
        try:
            return fn(arg)
        except ValueError as e:
            return 400, {"ok": False, "reason": str(e)}

    def attach_prompts(self, svc: PromptService, consts: Callable[[], dict[str, tuple]]) -> None:
        """B2：参数表和提示词编辑接口。consts() 返回只读常量 {key: (当前值, 默认值)}，每次请求现取。"""
        self.register_get("/api/params", lambda q: (200, {"ok": True, "items": describe(self.store.current, consts())}))
        self.register_get("/api/prompts", svc.list_all)
        self.register_get("/api/prompt", svc.get)
        self.register_post("/api/prompt", svc.post)

    def route_get(self, path: str, query: dict) -> tuple[int, dict] | None:
        """query 已拍平成 {键: 第一个值}。没注册这条路径返回 None。"""
        fn = self._get.get(path)
        return None if fn is None else self._call(fn, query)

    def route_post(self, path: str, body: dict) -> tuple[int, dict] | None:
        fn = self._post.get(path)
        return None if fn is None else self._call(fn, body)

    def html(self) -> bytes:
        """每次现读文件（改了页面刷新即可看到），把令牌填进占位符。"""
        return self.html_path.read_text(encoding="utf-8").replace("__GUANJIA_TOKEN__", self.token).encode("utf-8")

    def snapshot(self) -> dict:
        return {"status": self._status(), "settings": to_dict(self.store.current), "records": self.hub.records(),
                "logs": self.hub.logs(400), "live": dict(self.hub.live), "seq": self.hub.last_seq,
                "stages": list(STAGES), "stage_names": STAGE_CN, "log_modules": list(LOG_MODULES),
                "hidden_modules": list(DEFAULT_HIDDEN_MODULES)}

    def record(self, uid: str) -> dict | None:
        return self.hub.record(uid)

    def subscribe(self) -> queue.Queue:
        return self.hub.subscribe()

    def unsubscribe(self, q: queue.Queue) -> None:
        self.hub.unsubscribe(q)

    def events_since(self, seq: int) -> list[dict]:
        return self.hub.events_since(seq)

    def control(self, action: str) -> tuple[bool, str]:
        if action not in CONTROL_ACTIONS:
            return False, f"不认识的操作：{action}"
        return self._control(action)

    def update_settings(self, patch: dict) -> tuple[bool, dict | str]:
        try:
            return True, to_dict(self.store.update(patch))
        except ValueError as e:
            return False, str(e)
        except OSError as e:  # 文件被别的程序占着（杀毒、编辑器）：设置没变，告诉页面
            return False, f"写不进设置文件：{e}"

    def inject(self, text: str, dry_run: bool) -> dict:
        return self._inject(text, dry_run)
