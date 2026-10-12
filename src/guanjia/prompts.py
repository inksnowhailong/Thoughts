"""提示词登记表 + 模板渲染 + 面板用的读写服务。

kind：workspace（工作区整文件）/ block（AGENTS.md 里「管家规则」一段）/ template（我方代码渲染的模板）/
      readonly（只展示）/ setting（在参数里改）。

运行时只认工作区文件，所以面板改的是线上工作区文件；仓库里的副本（openclaw/…）当「默认值」，
面板显示「与默认不同」并可「恢复默认」，不会自动回写仓库。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from guanjia.wsfiles import Conflict, WorkspaceFiles, sha
from guanjia.wslock import LockBusy, ws_lock

log = logging.getLogger("guanjia.prompts")
REPO = Path(__file__).resolve().parents[2]
DEFAULT_DIR = Path(__file__).with_name("prompts")
OVERRIDE_DIR = REPO / "data" / "prompts"  # 覆盖版模板（不进仓库）；每次现读模块变量，测试可以改成临时目录
BLOCK_BEGIN, BLOCK_END = "<!-- guanjia-s2:begin -->", "<!-- guanjia-s2:end -->"
_PH = re.compile(r"\{\{([^{}]*)\}\}")
_WEEKDAY_CN = "一二三四五六日"
# 语音轮标记的校验规则，必须不比分级插件宽。插件剥标记用的是
#   openclaw/plugins/guanjia-guard/src/speak.ts：VOICE_TAG_RE = /^[\s\S]*\[语音[^\]]*\]\s*/
# （取最后一个「[语音 …]」及其前面的全部内容）。我们要求：整个标记是一行、以「[语音 」开头、以「]」结尾、
# 括号里不含 ] 和换行、最多 40 字，这样渲染结果一定被插件整段剥掉，AGENTS.md 的语音规则也认得。
VOICE_TAG_RULE = re.compile(r"\[语音 [^\]\r\n]{1,40}\]\Z")
PLACEHOLDERS = {"voice_tag": {"DATE": "日期，如 2026-10-11", "TIME": "时刻，如 18:05", "WEEKDAY": "周几（一～日）",
                              "SPEAKER": "声纹认出的说话人，前面自带一个空格，如「 主人」「 未知」；没有声纹结果时为空"}}
SPEAKER_MAX_CHARS = 8  # 说话人名字最多取这么多字：标记整体不超过 40 字，库里名字再长也不会让标记不合格

_BOOT_NOTE = "文档对启动文件注入时机有两种说法（每轮重建 / 新会话首轮）；上线后用「改一句规则→下一句是否遵守」实测，不遵守就开新会话"
_SKILL_NOTE = "OpenClaw 默认监听 SKILL.md（skills.load.watch 未关），改完下一轮自动刷新；监听失效时开新会话"


@dataclass(frozen=True)
class PromptEntry:
    id: str
    title: str
    node: str
    kind: str
    rel: str = ""
    repo_default: str = ""
    effect: str = "下一轮"
    budget: int = 0
    note: str = ""


def _skill(name: str, title: str) -> PromptEntry:
    return PromptEntry(f"skill_{name}", title, "tools", "workspace", rel=f"skills/guanjia-{name}/SKILL.md",
                       repo_default=f"openclaw/workspace/skills/guanjia-{name}/SKILL.md", note=_SKILL_NOTE)


PROMPTS: tuple[PromptEntry, ...] = (
    PromptEntry("soul", "人设 SOUL.md", "gateway", "workspace", rel="SOUL.md", budget=1500,
                note="目前还是 OpenClaw 英文默认模板，P4 再重写成散文人格。" + _BOOT_NOTE),
    PromptEntry("identity", "身份 IDENTITY.md", "gateway", "workspace", rel="IDENTITY.md", budget=1500, note=_BOOT_NOTE),
    PromptEntry("rules", "管家规则（语音回答、确认、技能路由）", "gateway", "block", rel="AGENTS.md",
                repo_default="openclaw/AGENTS-S2-section.md",
                note="只改 AGENTS.md 里两个标记之间的一段，其余部分不动。" + _BOOT_NOTE),
    PromptEntry("agents_rest", "AGENTS.md 其余（OpenClaw 默认）", "gateway", "readonly", rel="AGENTS.md",
                note="只读展示；管家规则那一段在上一项单独编辑"),
    PromptEntry("voice_tag", "语音轮标记", "gateway", "template", repo_default="src/guanjia/prompts/voice_tag.txt",
                note="每句语音前缀；分级插件和管家规则都认「[语音 …]」，只能改括号里的内容，渲染结果不合格会自动回退内置格式"),
    PromptEntry("guard", "分级插件注入的确认提示", "gateway", "readonly", effect="需重启网关",
                note="openclaw/plugins/guanjia-guard/src/guard.ts、habits.ts；改要重启网关并重新授权，本期只读"),
    _skill("notes", "技能：记事与提醒"),
    _skill("lookup", "技能：查询"),
    _skill("launch", "技能：启动应用"),
    PromptEntry("ack", "唤醒应答", "wake", "setting", effect="立即", note="设置项 ack_text，在参数表里改"),
)
_BY_ID = {e.id: e for e in PROMPTS}


def render(template: str, ctx: dict[str, str]) -> tuple[str, list[str]]:
    """替换 {{占位符}}；返回 (结果, 未知占位符)。未知的原样留着。"""
    unknown: list[str] = []

    def sub(m: re.Match) -> str:
        if m.group(1) in ctx:
            return ctx[m.group(1)]
        unknown.append(m.group(1))
        return m.group(0)

    return _PH.sub(sub, template), unknown


def _speaker_part(speaker: str) -> str:
    """说话人 → 标记里的片段：" 主人"；空 / 清洗后为空返回 ""。去掉 [ ] 和空白，防止破坏插件的正则 [语音[^\]]*\]。"""
    name = re.sub(r"[\[\]\s]+", "", speaker or "")[:SPEAKER_MAX_CHARS]
    return f" {name}" if name else ""


def voice_ctx(now: datetime, speaker: str = "") -> dict[str, str]:
    return {"DATE": f"{now:%Y-%m-%d}", "TIME": f"{now:%H:%M}", "WEEKDAY": _WEEKDAY_CN[now.weekday()],
            "SPEAKER": _speaker_part(speaker)}


def _builtin_voice_tag(now: datetime, speaker: str = "") -> str:
    return f"[语音 {now:%Y-%m-%d %H:%M} 周{_WEEKDAY_CN[now.weekday()]}{_speaker_part(speaker)}]"


def load_template(pid: str) -> tuple[str, bool]:
    """(模板文本, 是否被覆盖)。覆盖版在 data/prompts/<id>.txt。"""
    o = OVERRIDE_DIR / f"{pid}.txt"
    if o.exists():
        return o.read_text(encoding="utf-8-sig").strip("\r\n"), True
    return (DEFAULT_DIR / f"{pid}.txt").read_text(encoding="utf-8-sig").strip("\r\n"), False


def check_template(pid: str, text: str) -> str:
    """校验并返回渲染预览；不合格抛 ValueError。"""
    sample = datetime(2026, 10, 11, 18, 5)
    rendered, unknown = render(text, voice_ctx(sample, "主人") if pid == "voice_tag" else {})
    if unknown:
        raise ValueError(f"不认识的占位符：{', '.join(unknown)}（可用：{', '.join(PLACEHOLDERS.get(pid, {}))}）")
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("{{ }} 不成对")
    if pid == "voice_tag":
        no_speaker = render(text, voice_ctx(sample))[0]  # 没有声纹结果时（说话人为空）也得合格
        if not (VOICE_TAG_RULE.match(rendered) and VOICE_TAG_RULE.match(no_speaker)):
            raise ValueError("语音标记必须是一行、以「[语音 」开头、以「]」结尾、括号里不超过 40 字（有无说话人都要满足）")
    return rendered


def save_template(pid: str, text: str) -> None:
    from guanjia.fsutil import atomic_write_text

    text = text.strip("\r\n")
    check_template(pid, text)
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(OVERRIDE_DIR / f"{pid}.txt", text)


def reset_template(pid: str) -> None:
    (OVERRIDE_DIR / f"{pid}.txt").unlink(missing_ok=True)


def render_voice_tag(now: datetime | None = None, speaker: str = "") -> str:
    """brain.voice_tag 用。speaker 是声纹认出的说话人（"" = 没有）。覆盖文件被手工改坏、读不了时回退内置格式（保护插件和语音规则）。
    默认模板渲染结果与内置格式逐字节相同；speaker 为空时渲染结果与加说话人之前逐字节一致。"""
    now = now or datetime.now()
    try:
        text, _ = load_template("voice_tag")
        out, unknown = render(text, voice_ctx(now, speaker))
        if not unknown and VOICE_TAG_RULE.match(out):
            return out
        log.warning("voice_tag 模板不合格，用内置格式：%r", text)
    except (OSError, ValueError) as e:  # ValueError 含 UnicodeDecodeError
        log.warning("voice_tag 模板读不了，用内置格式：%s", e)
    return _builtin_voice_tag(now, speaker)


def split_block(text: str) -> tuple[str, str, str]:
    """AGENTS.md → (段前含开始标记, 段内, 结束标记含段后)。标记缺失或重复抛 ValueError。"""
    if text.count(BLOCK_BEGIN) != 1 or text.count(BLOCK_END) != 1 or text.index(BLOCK_BEGIN) > text.index(BLOCK_END):
        raise ValueError("AGENTS.md 里管家规则的开始/结束标记不完整，先人工修好")
    a = text.index(BLOCK_BEGIN) + len(BLOCK_BEGIN)
    b = text.index(BLOCK_END)
    return text[:a], text[a:b], text[b:]


_FRONT = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.S)


def _skill_name(text: str) -> str | None:
    m = _FRONT.match(text)
    n = re.search(r"^name:\s*(.+?)\s*$", m.group(1), re.M) if m else None
    return n.group(1) if n else None


def check_skill(old: str | None, new: str) -> None:
    """SKILL.md 的 frontmatter 被改坏会让技能失效：开头必须是 --- 包起来的块、有 name:，且 name 不许变。"""
    name = _skill_name(new)
    if name is None:
        raise ValueError("SKILL.md 开头必须是 --- 包起来的 frontmatter，并且要有 name: 一行")
    old_name = _skill_name(old) if old is not None else None
    if old_name is not None and old_name != name:
        raise ValueError(f"不能改技能名 name（现在是 {old_name}）")


def entries() -> list[dict]:
    return [asdict(e) for e in PROMPTS]


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


class PromptService:
    """面板「提示词」接口的实现。路由函数返回 (状态码, JSON)；panel.attach_prompts 把它们接到路由表上。"""

    def __init__(self, fs: WorkspaceFiles, repo: Path = REPO, now: Callable[[], datetime] = datetime.now,
                 lock_timeout: float = 10.0) -> None:
        self.fs = fs
        self.repo = repo
        self._now = now
        self.lock_timeout = lock_timeout  # 等写锁最多多久，超了回 423

    # ---------- 读 ----------
    def _repo_text(self, rel: str) -> str | None:
        p = self.repo / rel
        return _norm(p.read_bytes().decode("utf-8-sig")) if p.exists() else None

    def _default_text(self, e: PromptEntry) -> str | None:
        """仓库默认值；没有就是 None。block 取标记之间的一段。"""
        if not e.repo_default:
            return None
        if e.kind == "template":
            return (DEFAULT_DIR / f"{e.id}.txt").read_text(encoding="utf-8-sig").strip("\r\n")
        text = self._repo_text(e.repo_default)
        if text is None:
            return None
        if e.kind == "block" and BLOCK_BEGIN in text:
            return split_block(text)[1].strip("\n")
        return text.strip("\n") if e.kind == "block" else text

    def _current(self, e: PromptEntry) -> dict:
        """当前内容：{text, sha, exists, history, overridden?}。sha 是编辑时要带回来的 base_sha。"""
        if e.kind == "workspace":
            r = self.fs.read(e.rel)
            return {"text": r["text"], "sha": r["sha"], "exists": r["exists"], "history": self.fs.history(e.rel)}
        if e.kind == "block":
            r = self.fs.read(e.rel)
            if not r["exists"]:
                raise ValueError("AGENTS.md 不存在")
            return {"text": split_block(r["text"])[1].strip("\n"), "sha": r["sha"], "exists": True,
                    "history": self.fs.history(e.rel)}
        if e.kind == "template":
            text, overridden = load_template(e.id)
            return {"text": text, "sha": sha(text), "exists": True, "history": [], "overridden": overridden}
        if e.id == "agents_rest":
            r = self.fs.read(e.rel)
            try:
                pre, _, post = split_block(r["text"])
                text = pre + "\n（管家规则那一段在上一项单独编辑，这里不显示）\n" + post
            except ValueError:
                text = r["text"]
            return {"text": text, "sha": r["sha"], "exists": r["exists"], "history": []}
        if e.id == "guard":
            parts = []
            for name in ("guard.ts", "habits.ts"):
                p = self.repo / "openclaw" / "plugins" / "guanjia-guard" / "src" / name
                if p.exists():
                    parts.append(f"//// {name}（前 4000 字）\n" + p.read_text(encoding="utf-8-sig")[:4000])
            text = "\n\n".join(parts) or "（读不到插件源码）"
            return {"text": text, "sha": sha(text), "exists": bool(parts), "history": []}
        raise ValueError("这一项在参数表里改")

    @staticmethod
    def _editable(e: PromptEntry) -> bool:
        return e.kind in ("workspace", "block", "template")

    def _brief(self, e: PromptEntry) -> dict:
        d = {"id": e.id, "title": e.title, "node": e.node, "kind": e.kind, "rel": e.rel, "effect": e.effect,
             "budget": e.budget, "note": e.note, "editable": self._editable(e), "chars": None, "sha": "",
             "exists": None, "differs_from_default": None, "error": None}
        if e.kind == "setting":
            return d
        try:
            cur = self._current(e)
        except ValueError as ex:
            d["error"] = str(ex)
            d["editable"] = False
            return d
        d.update(chars=len(cur["text"]), sha=cur["sha"], exists=cur["exists"])
        default = self._default_text(e) if self._editable(e) else None
        if default is not None:
            d["differs_from_default"] = cur["text"].strip() != default.strip()
        return d

    def list_all(self, query: dict | None = None) -> tuple[int, dict]:
        return 200, {"ok": True, "items": [self._brief(e) for e in PROMPTS], "placeholders": PLACEHOLDERS}

    def _entry(self, pid) -> PromptEntry:
        e = _BY_ID.get(str(pid or ""))
        if e is None:
            raise ValueError(f"不认识的提示词：{pid}")
        return e

    def _detail(self, e: PromptEntry) -> dict:
        cur = self._current(e)
        d = self._brief(e)
        default = self._default_text(e) if self._editable(e) else None
        d.update(text=cur["text"], sha=cur["sha"], exists=cur["exists"], history=cur["history"], default_text=default,
                 overridden=cur.get("overridden"), preview=None, unknown=[], valid=True, reason="",
                 placeholders=PLACEHOLDERS.get(e.id, {}))
        if e.kind == "template":
            d.update(self._preview(e, cur["text"]))
        return d

    def _preview(self, e: PromptEntry, text: str) -> dict:
        """模板的渲染预览（用当前时间）+ 是否合格。不合格不抛，把原因带回去给页面标红。"""
        rendered, unknown = render(text, voice_ctx(self._now(), "主人") if e.id == "voice_tag" else {})
        try:
            check_template(e.id, text)
            return {"preview": rendered, "unknown": unknown, "valid": True, "reason": ""}
        except ValueError as ex:
            return {"preview": rendered, "unknown": unknown, "valid": False, "reason": str(ex)}

    def get(self, query: dict) -> tuple[int, dict]:
        return self._guard(lambda: self._get(query))

    def _get(self, query: dict) -> tuple[int, dict]:
        e = self._entry(query.get("id"))
        d = self._detail(e)
        version = query.get("version")
        if version:  # 预览某个历史版本（block 只取管家规则那一段）
            old = self.fs.history_text(e.rel, version)
            d["version_text"] = split_block(old)[1].strip("\n") if e.kind == "block" else old
        return 200, {"ok": True, **d}

    # ---------- 写 ----------
    def post(self, body: dict) -> tuple[int, dict]:
        return self._guard(lambda: self._post(body))

    @staticmethod
    def _guard(fn: Callable[[], tuple[int, dict]]) -> tuple[int, dict]:
        try:
            return fn()
        except Conflict:
            return 409, {"ok": False, "reason": "文件刚被改过（多半是模型刚写过），你的修改还在编辑框里，请选择重新载入或用你的版本覆盖"}
        except LockBusy as ex:
            return 423, {"ok": False, "reason": f"休眠整理或别处正在写工作区，稍后再存（占用者：{ex}）"}
        except ValueError as ex:
            return 400, {"ok": False, "reason": str(ex)}
        except OSError as ex:
            return 500, {"ok": False, "reason": f"读写文件失败：{ex}"}

    def _write_block(self, e: PromptEntry, text: str, base_sha: str) -> None:
        """锁内重读整文件 → 只换两个标记之间的一段 → 拼回写入。base_sha 是打开时整个文件的 sha。"""
        text = _norm(text).strip("\n")
        if not text.strip():
            raise ValueError("内容不能为空")
        if BLOCK_BEGIN in text or BLOCK_END in text:
            raise ValueError("管家规则正文里不能出现 guanjia-s2 的开始/结束标记")
        with ws_lock(timeout=self.lock_timeout, holder="panel", path=self.fs.lock_path):
            cur = self.fs.read(e.rel)
            if not cur["exists"]:
                raise ValueError("AGENTS.md 不存在")
            if cur["sha"] != base_sha:
                raise Conflict(e.rel)
            pre, inner, post = split_block(cur["text"])
            new_inner = inner if text == inner.strip("\n") else "\n" + text + "\n"  # 没改就原样保留段内空白
            self.fs.write_unlocked(e.rel, pre + new_inner + post, base_sha)

    def _write(self, e: PromptEntry, text: str, base_sha: str) -> None:
        if e.kind == "workspace":
            self.fs.write(e.rel, text, base_sha, timeout=self.lock_timeout,
                          validate=check_skill if e.rel.endswith("SKILL.md") else None)
        elif e.kind == "block":
            self._write_block(e, text, base_sha)
        else:
            save_template(e.id, text)

    def _post(self, body: dict) -> tuple[int, dict]:
        e = self._entry(body.get("id"))
        if not self._editable(e):
            raise ValueError("这一项是只读的" if e.kind == "readonly" else "这一项在参数表里改")
        action = str(body.get("action") or "save")
        base_sha = str(body.get("base_sha") or "")
        if action == "preview":
            if e.kind != "template":
                raise ValueError("只有模板有预览")
            return 200, {"ok": True, **self._preview(e, str(body.get("text") or "").strip("\r\n"))}
        if action == "save":
            text = body.get("text")
            if not isinstance(text, str):
                raise ValueError("缺少 text")
            if not text.strip():  # 清空等于把提示词删了，不当成一次正常保存
                raise ValueError("内容不能为空")
            self._write(e, text, base_sha)
        elif action == "reset":
            if e.kind == "template":
                reset_template(e.id)
            else:
                default = self._default_text(e)
                if default is None:
                    raise ValueError("这一项没有仓库默认值")
                self._write(e, default, base_sha)
        elif action == "restore":
            if e.kind == "template":
                raise ValueError("模板没有历史版本，用「恢复默认」")
            old = self.fs.history_text(e.rel, str(body.get("version") or ""))
            self._write(e, split_block(old)[1].strip("\n") if e.kind == "block" else old, base_sha)
        else:
            raise ValueError(f"不认识的操作：{action}")
        return 200, {"ok": True, "saved": True, "effect": e.effect, **self._detail(e)}
