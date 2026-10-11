"""B3 记忆页后端：USER / MEMORY / 每日笔记 / 时间线 / 家庭事实的读写接口（全部走 WorkspaceFiles：同一把写锁、
冲突检测、历史备份、审计）。路由函数返回 (状态码, JSON)，由 panel.attach_memory 接到路由表上。

只认下面这张表里的文件和 memory/YYYY-MM-DD.md；SOUL / AGENTS 等提示词文件在「环节抽屉」里改，不在这里。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from guanjia.prompts import PromptService
from guanjia.wsfiles import _DAILY, WorkspaceFiles

TIMELINE_WARN = "技能约定时间线只追加：你改动了旧行，会让「我之前说过啥」对不上（仍然保存了，旧版在历史里可恢复）"
FACTS_NOTE = "脱敏代理的实体表（人名/房间/设备/路径/IP）。只许在每个数组末尾追加：不能删项、改顺序、删键，也不能改「_说明」；改完即时生效"
_CARD = re.compile(r"<!--\s*observed:\s*([^|>]*?)\s*\|\s*status:\s*([A-Za-z_]+)\s*-->")


@dataclass(frozen=True)
class MemFile:
    rel: str
    title: str
    kind: str  # user / md / timeline / facts / daily
    budget: int = 0  # 软预算（字）；0 = 不设
    effect: str = "下一轮"
    note: str = ""


MEMORY_FILES: tuple[MemFile, ...] = (
    MemFile("USER.md", "用户档案 USER.md", "user", 3600, "下一轮", "每轮注入；下面的卡片按 observed / status 注释解析，编辑看原文"),
    MemFile("MEMORY.md", "长期记忆 MEMORY.md", "md", 6000, "下一轮", "只在主线会话加载；还没有就直接写一条然后保存来新建"),
    MemFile("memory/timeline.md", "时间线 timeline.md", "timeline", 0, "模型下次读取时", "⚠ " + TIMELINE_WARN),
    MemFile("guanjia/family-facts.json", "家庭事实 family-facts.json", "facts", 0, "即时", FACTS_NOTE),
)
_BY_REL = {f.rel: f for f in MEMORY_FILES}


def parse_user_cards(text: str) -> list[dict]:
    """USER.md → 卡片：`<!-- observed: 日期 | status: active/superseded -->` 加紧随其后的第一条 `- ` 指令。
    日期里还留着 YYYY 的是模板示例，标成占位。"""
    lines = text.split("\n")
    cards = []
    for i, line in enumerate(lines):
        m = _CARD.search(line)
        if not m:
            continue
        body = ""
        for x in lines[i + 1:]:  # 只看到下一个注释之前：不把下一张卡片的指令当成这一张的
            if _CARD.search(x):
                break
            if x.strip().startswith("- "):
                body = x.strip()[2:].strip()
                break
        date = m.group(1).strip()
        cards.append({"date": date, "status": m.group(2).strip(), "text": body, "placeholder": "YYYY" in date.upper()})
    return cards


def check_facts(old: str | None, new: str) -> None:
    """family-facts.json 只许末尾追加：合法 JSON 对象；旧的每个键都在；数组旧内容是新数组的前缀；
    「_说明」和其它非数组值不许改。不合格抛带中文原因的 ValueError。"""
    try:
        nd = json.loads(new)
    except ValueError as e:
        raise ValueError(f"不是合法 JSON：{e}") from e
    if not isinstance(nd, dict):
        raise ValueError("顶层必须是 JSON 对象")
    if old is None or not old.strip():
        return  # 文件还不存在：新建，合法即可
    try:
        od = json.loads(old)
    except ValueError:
        return  # 线上旧文件本身就坏了：不卡住修复，合法 JSON 即可
    if not isinstance(od, dict):
        return
    for key, ov in od.items():
        if key not in nd:
            raise ValueError(f"不能删除「{key}」")
        nv = nd[key]
        if isinstance(ov, list):
            if not isinstance(nv, list):
                raise ValueError(f"「{key}」必须仍然是数组")
            if nv[:len(ov)] != ov:
                raise ValueError(f"「{key}」只许在末尾追加：原有的项不能删、不能改、不能换顺序")
        elif nv != ov:
            raise ValueError(f"「{key}」不许修改" + ("（说明文字）" if key == "_说明" else "，这里只许在数组末尾追加"))


class MemoryService:
    def __init__(self, fs: WorkspaceFiles, log=None, lock_timeout: float = 10.0) -> None:
        self.fs = fs
        self.log = log  # elog.EventLog / NullLog；None 时不记事件（审计仍由 WorkspaceFiles 写）
        self.lock_timeout = lock_timeout

    # ---------- 读 ----------
    @staticmethod
    def _entry(rel: str) -> MemFile:
        rel = str(rel or "").replace("\\", "/")
        if rel in _BY_REL:
            return _BY_REL[rel]
        if _DAILY.match(rel):
            return MemFile(rel, f"每日笔记 {rel[7:-3]}", "daily", 0, "新会话启动时带最近几天", "")
        raise ValueError(f"记忆页不管这个文件：{rel}")

    def _brief(self, e: MemFile) -> dict:
        r = self.fs.read(e.rel)
        return {"rel": e.rel, "title": e.title, "kind": e.kind, "budget": e.budget, "effect": e.effect, "note": e.note,
                "exists": r["exists"], "chars": r["chars"], "mtime": r["mtime"]}

    def list_all(self, query: dict | None = None) -> tuple[int, dict]:
        return self._guard(lambda: (200, {"ok": True, "items": [self._brief(e) for e in MEMORY_FILES],
                                          "daily": [self._brief(self._entry(rel)) for rel in self.fs.daily_notes(30)]}))

    def _detail(self, e: MemFile, cur: dict | None = None) -> dict:
        cur = cur or self.fs.read(e.rel)
        d = {"rel": e.rel, "title": e.title, "kind": e.kind, "budget": e.budget, "effect": e.effect, "note": e.note,
             "exists": cur["exists"], "text": cur["text"], "sha": cur["sha"], "chars": cur["chars"],
             "history": self.fs.history(e.rel)}
        if e.kind == "user":
            d["cards"] = parse_user_cards(cur["text"])
        return d

    def get(self, query: dict) -> tuple[int, dict]:
        def run() -> tuple[int, dict]:
            e = self._entry(query.get("rel"))
            d = self._detail(e)
            if query.get("version"):  # 预览某个历史版本
                d["version_text"] = self.fs.history_text(e.rel, str(query["version"]))
            return 200, {"ok": True, **d}

        return self._guard(run)

    # ---------- 写 ----------
    def post(self, body: dict) -> tuple[int, dict]:
        return self._guard(lambda: self._post(body))

    def _post(self, body: dict) -> tuple[int, dict]:
        e = self._entry(body.get("rel"))
        action = str(body.get("action") or "save")
        base_sha = str(body.get("base_sha") or "")
        if action == "save":
            text = body.get("text")
            if not isinstance(text, str):
                raise ValueError("缺少 text")
        elif action == "restore":
            text = self.fs.history_text(e.rel, str(body.get("version") or ""))
        else:
            raise ValueError(f"不认识的操作：{action}")
        touched = {"old_lines": False}

        def validate(old: str | None, new: str) -> None:
            if e.kind == "facts":
                check_facts(old, new)
            if e.kind == "timeline" and old and not new.startswith(old):
                touched["old_lines"] = True

        cur = self.fs.write(e.rel, text, base_sha, timeout=self.lock_timeout, validate=validate)
        if self.log is not None:
            self.log.emit("panel", "memory.save", f"记忆页保存：{e.rel}", rel=e.rel, action=action, chars=cur["chars"],
                          touched_old_lines=touched["old_lines"])
        out = {"ok": True, "saved": True, **self._detail(e, cur)}
        if touched["old_lines"]:
            out["warn"] = TIMELINE_WARN
        return 200, out

    _guard: Callable = staticmethod(PromptService._guard)  # 同一套错误映射：409 冲突 / 423 锁被占 / 400 校验 / 500 读写失败
