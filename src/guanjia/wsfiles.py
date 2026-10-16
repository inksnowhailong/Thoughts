"""OpenClaw 工作区文本文件的安全读写（面板提示词页、记忆页；P4 休眠提交复用）。

    读：返回规范成 LF 的文本 + sha（编辑器用）
    写：拿写锁 → 重读比对 base_sha（模型可能刚写过）→ 校验 → 备份旧版 → 原子替换（保持原换行风格）→ 审计

给 P4 的复用约定（休眠提交）：
    with ws_lock(holder="sleep", timeout=60):
        对每个文件 fs.read 取 sha → 生成新内容 → fs.write_unlocked(rel, text, sha, actor="sleep")
    遇 Conflict 说明模型在整理期间写过：放弃本次提交，下次休眠重来。
    面板保存时若 P4 正持锁，面板等 10 秒后得到 LockBusy（路由层回 423）。
任何改工作区文本的我方代码都必须在这把锁里写（直接调 write() 就是自动拿锁）。

历史版本放在仓库外的 data/ws-history/<路径把 / 换成 __>/，每个文件留最近 20 份；不放进工作区，免得被 OpenClaw 当文件读。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from guanjia.fsutil import atomic_write_text
from guanjia.wslock import ws_lock

log = logging.getLogger("guanjia.wsfiles")
WORKSPACE = Path.home() / ".openclaw" / "workspace"
HISTORY_DIR = Path(__file__).resolve().parents[2] / "data" / "ws-history"
HARD_MAX_CHARS = 20000  # OpenClaw 单个启动文件超出会被截断
HISTORY_KEEP = 20
# 只认登记过的相对路径和每日笔记；B3 的记忆页复用同一张表
ALLOWED = ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md", "MEMORY.md", "memory/index.md", "memory/timeline.md",
           "skills/guanjia-notes/SKILL.md", "skills/guanjia-lookup/SKILL.md", "skills/guanjia-launch/SKILL.md",
           "guanjia/family-facts.json")
_DAILY = re.compile(r"^memory/\d{4}-\d{2}-\d{2}\.md$")
_PERSON = re.compile(r"^memory/people/(主人|老婆)\.md$")
_TOPIC = re.compile(r"^memory/topics/[A-Za-z0-9._-]+\.md$")
_VERSION = re.compile(r"\d{8}-\d{6}-\d{9}")


class Conflict(Exception):
    """文件在打开之后被改过（多半是模型刚写过）。"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class WorkspaceFiles:
    def __init__(self, root: Path = WORKSPACE, history_dir: Path = HISTORY_DIR, lock_path: Path | None = None) -> None:
        self.root = root
        self.history_dir = history_dir
        # 锁文件默认跟着工作区走（线上就是 workspace/guanjia/ws-write.lock），测试用临时工作区时不会碰线上的锁
        self.lock_path = lock_path or root / "guanjia" / "ws-write.lock"

    def resolve(self, rel: str) -> Path:
        """白名单 + 必须仍在工作区目录下；不合格抛 ValueError。"""
        rel = rel.replace("\\", "/")
        if rel not in ALLOWED and not _DAILY.match(rel) and not _PERSON.match(rel) and not _TOPIC.match(rel):
            raise ValueError(f"不允许的文件：{rel}")
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError(f"不允许的文件：{rel}")
        return p

    @staticmethod
    def _raw(p: Path) -> str | None:
        return p.read_bytes().decode("utf-8-sig") if p.exists() else None

    def read(self, rel: str) -> dict:
        p = self.resolve(rel)
        raw = self._raw(p)
        text = (raw or "").replace("\r\n", "\n")
        return {"rel": rel, "exists": raw is not None, "text": text, "sha": sha(raw) if raw is not None else "",
                "chars": len(text), "mtime": p.stat().st_mtime if raw is not None else None}

    def write(self, rel: str, text: str, base_sha: str, *, actor: str = "panel", timeout: float = 10.0,
              validate: Callable[[str | None, str], None] | None = None) -> dict:
        with ws_lock(timeout=timeout, holder=actor, path=self.lock_path):
            return self.write_unlocked(rel, text, base_sha, actor=actor, validate=validate)

    def write_unlocked(self, rel: str, text: str, base_sha: str, *, actor: str = "panel",
                       validate: Callable[[str | None, str], None] | None = None) -> dict:
        """调用方必须已持有 ws_lock。validate(旧文本或 None, 新文本) 不通过就抛 ValueError。"""
        p = self.resolve(rel)
        text = text.replace("\r\n", "\n")
        if len(text) > HARD_MAX_CHARS:
            raise ValueError(f"太长了：{len(text)} 字，上限 {HARD_MAX_CHARS}")
        raw = self._raw(p)
        if (sha(raw) if raw is not None else "") != base_sha:
            raise Conflict(rel)
        old = raw.replace("\r\n", "\n") if raw is not None else None
        if validate:
            validate(old, text)
        if old == text:
            return self.read(rel)
        if raw is not None and "\r\n" in raw and "\n" in raw.replace("\r\n", ""):
            # CRLF 和 LF 混用的文件：统一换行会改动我们没编辑的行，宁可拒绝
            raise ValueError(f"{rel} 的换行风格混用（CRLF 和 LF 都有），先人工统一再用面板改")
        # P4-6c 的新长期文件也保留一个空白前像，确保每个提交目标都有可追溯历史。
        self._backup(rel, raw or "")
        out = text.replace("\n", "\r\n") if raw is not None and "\r\n" in raw else text  # 保持原文件换行风格
        p.parent.mkdir(parents=True, exist_ok=True)
        # 备份花了点时间，模型这期间可能又写了文件：替换前最后比对一次（剩下的只有毫秒级窗口）
        again = self._raw(p)
        if (sha(again) if again is not None else "") != base_sha:
            raise Conflict(rel)
        atomic_write_text(p, out)
        try:  # 文件已经替换成功，审计写失败不能让接口报失败，否则主人以为没存上
            self._audit(rel, actor, sha(raw) if raw is not None else "", sha(out), len(text))
        except OSError as e:
            log.warning("审计写入失败（%s 已更新）：%s", rel, e)
        return self.read(rel)

    def _hist_dir(self, rel: str) -> Path:
        return self.history_dir / rel.replace("/", "__")

    def _backup(self, rel: str, raw: str) -> None:
        d = self._hist_dir(rel)
        d.mkdir(parents=True, exist_ok=True)
        # 文件名 = 秒级时间 + 纳秒小数部分，字典序就是时间序
        ns = time.time_ns()  # 只取一次时间，免得秒和小数部分跨秒错位
        name = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(ns // 10**9))}-{ns % 10**9:09d}.txt"
        (d / name).write_text(raw, encoding="utf-8", newline="")
        for f in sorted(d.glob("*.txt"))[:-HISTORY_KEEP]:
            f.unlink(missing_ok=True)

    def _audit(self, rel: str, actor: str, old: str, new: str, chars: int) -> None:
        """审计只记谁改了哪个文件、前后 sha、字数，不记内容。"""
        self.history_dir.mkdir(parents=True, exist_ok=True)
        line = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "file": rel, "actor": actor, "old": old, "new": new, "chars": chars}
        with open(self.history_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def history(self, rel: str) -> list[dict]:
        """最新的在前。"""
        self.resolve(rel)
        d = self._hist_dir(rel)
        files = sorted(d.glob("*.txt"), reverse=True) if d.exists() else []
        return [{"id": f.stem, "t": f.stem[:15], "bytes": f.stat().st_size} for f in files]

    def history_text(self, rel: str, version: str) -> str:
        self.resolve(rel)
        if not _VERSION.fullmatch(version):
            raise ValueError("版本号不对")
        f = self._hist_dir(rel) / f"{version}.txt"
        if not f.exists():
            raise ValueError("没有这个历史版本")
        return f.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")

    def daily_notes(self, limit: int = 30) -> list[str]:
        d = self.root / "memory"
        names = sorted((f.name for f in d.glob("*.md") if _DAILY.match(f"memory/{f.name}")), reverse=True) if d.exists() else []
        return [f"memory/{n}" for n in names[:limit]]
