"""「管家面板」的后端：把守护进程的状态、每句话的记录、事件流、控制和设置接到控制口上。

页面是同目录的 panel.html（单个静态文件，不引外部资源，离线可用）。每次守护进程启动随机生成一个令牌，
只写进 /panel 返回的页面里；控制口要求 /api/* 带上它（见 control.py 的说明）。
"""

from __future__ import annotations

import json
import queue
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from guanjia import elog
from guanjia.events import LOG_MODULES, STAGE_CN, STAGES, EventHub
from guanjia.params import describe
from guanjia.prompts import PromptService
from guanjia.remembered_overrides import LEVELS_PATH, list_remembered, revoke
from guanjia.settings import SettingsStore, to_dict
from guanjia.wsfiles import HISTORY_DIR

PANEL_HTML = Path(__file__).with_name("panel.html")
PANEL_URL = "http://127.0.0.1:18795/panel"
CONTROL_ACTIONS = ("mute", "unmute", "restart", "quit")
DEFAULT_HIDDEN_MODULES = ("health", "barge-mon")  # 面板日志默认不显示的高频行


class PanelBackend:
    def __init__(self, hub: EventHub, store: SettingsStore, *, status: Callable[[], dict],
                 control: Callable[[str], tuple[bool, str]], inject: Callable[[str, bool], dict],
                 html_path: Path = PANEL_HTML, token: str | None = None,
                 levels_path: Path = LEVELS_PATH, audit_dir: Path = HISTORY_DIR) -> None:
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
        self._elog: elog.EventLog | elog.NullLog = elog.NullLog()  # 统一事件日志；attach_flags 时接上，之前是空操作
        self.levels_path = levels_path  # 线上分级表（测试传临时文件）
        self.audit_dir = audit_dir  # 撤销审计写到这里的 audit.jsonl（与工作区文件改动同一份审计）
        self.register_get("/api/overrides", self._overrides_list)
        self.register_post("/api/overrides", self._overrides_post)

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

    # ---------- B4：「已记住的放行」列表与撤销 ----------
    def _overrides_list(self, query: dict) -> tuple[int, dict]:
        try:
            return 200, {"ok": True, "items": list_remembered(self.levels_path)}
        except ValueError as e:  # 分级表读不出来：不当成「没有」，让页面显示原因
            return 500, {"ok": False, "reason": str(e)}

    def _overrides_post(self, body: dict) -> tuple[int, dict]:
        """只有 revoke：只删 source=remembered 的条目（手写的、rules、tools 一律不动），结果映射成 HTTP 状态码。"""
        if body.get("action") != "revoke":
            raise ValueError(f"不认识的操作：{body.get('action')}")
        oid = str(body.get("id") or "").strip()
        if not oid:
            raise ValueError("缺少 id")
        try:
            summary = next((i.get("summary") for i in list_remembered(self.levels_path) if i["id"] == oid), None)
        except ValueError as e:
            return 500, {"ok": False, "reason": str(e)}
        ok, why = revoke(oid, self.levels_path)
        if not ok:
            code, reason = {"not_found": (404, "没有这一条（可能已经被撤销）"),
                            "not_remembered": (403, "这一条不是口头记住的，面板不能撤销"),
                            "unreadable": (500, "分级表读不出来，没有改动")}[why]
            return code, {"ok": False, "reason": reason}
        self._audit_revoke(oid, summary)
        self._elog.emit("panel", "panel.override_revoke", f"撤销放行：{summary or oid}", id=oid, summary=summary)
        return 200, {"ok": True}

    def _audit_revoke(self, oid: str, summary: str | None) -> None:
        """撤销已经成功，审计写失败不能让接口报失败（主人会以为没撤掉）。"""
        line = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "file": "guanjia/levels.json", "actor": "panel", "id": oid, "summary": summary}
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            with open(self.audit_dir / "audit.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def attach_prompts(self, svc: PromptService, consts: Callable[[], dict[str, tuple]]) -> None:
        """B2：参数表和提示词编辑接口。consts() 返回只读常量 {key: (当前值, 默认值)}，每次请求现取。"""
        self.register_get("/api/params", lambda q: (200, {"ok": True, "items": describe(self.store.current, consts())}))
        self.register_get("/api/prompts", svc.list_all)
        self.register_get("/api/prompt", svc.get)
        self.register_post("/api/prompt", svc.post)

    def attach_flags(self, log: elog.EventLog | elog.NullLog) -> None:
        """面板每句话旁的「这句有问题」：写一条 user_flag 事件（logq --flags 列出），并在记录上标记。
        同时让改设置 / 控制（静音、重启、退出）也进事件流（panel.settings / panel.control）。"""
        self._elog = log

        def flag(body: dict) -> tuple[int, dict]:
            uid = str(body.get("uid") or "").strip()[:40]
            note = str(body.get("note") or "").strip()[:200]
            if not uid:
                raise ValueError("缺少 uid")
            rec = self.hub.record(uid) or {}  # 面板只留最近 50 句，老的查不到也照样记
            log.emit("panel", "user_flag", note or "（没写备注）", level="warn", uid=uid, text=(rec.get("text") or "")[:80] or None,
                     reply=(rec.get("reply") or "")[:160] or None, verdict=rec.get("verdict") or None)
            self.hub.update(uid, flag=note or "有问题")
            return 200, {"ok": True}

        self.register_post("/api/flag", flag)

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
        self._elog.emit("panel", "panel.control", f"面板：{action}", action=action)
        return self._control(action)

    def update_settings(self, patch: dict) -> tuple[bool, dict | str]:
        try:
            new = to_dict(self.store.update(patch))
            self._elog.emit("panel", "panel.settings", "面板改设置：" + ",".join(patch), changed=list(patch), patch=patch)
            return True, new
        except ValueError as e:
            return False, str(e)
        except OSError as e:  # 文件被别的程序占着（杀毒、编辑器）：设置没变，告诉页面
            return False, f"写不进设置文件：{e}"

    def inject(self, text: str, dry_run: bool) -> dict:
        return self._inject(text, dry_run)
