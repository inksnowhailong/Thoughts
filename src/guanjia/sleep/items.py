"""分层记忆 Markdown 的纯解析与渲染。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_META = re.compile(r"<!--\s*(.*?)\s*-->")
_ITEM = re.compile(r"^\s*-\s+(.*?)(?:\s+<!--\s*(.*?)\s*-->)?\s*$")
_HEADING = re.compile(r"^##\s+(.+?)\s*$")


@dataclass(frozen=True)
class MemoryItem:
    """一条带行尾元数据的记忆；未带元数据的普通列表同样可解析。"""

    text: str
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FileHeader:
    """L3 文件前三行和文件级元数据。"""

    title: str
    summary: str
    keywords: tuple[str, ...]
    meta: dict[str, str]


def parse_meta(text: str) -> dict[str, str]:
    """解析 `key: value | key: value`，未知字段原样保留。"""
    out: dict[str, str] = {}
    for part in text.split("|"):
        key, sep, value = part.strip().partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def metadata_comments(text: str) -> list[dict[str, str]]:
    """返回所有 HTML 注释中的键值元数据，供 layer/source/visibility 保真使用。"""
    return [parse_meta(match.group(1)) for match in _META.finditer(text) if ":" in match.group(1)]


def split_sections(text: str) -> list[tuple[str, str]]:
    """按二级标题分节，正文不含标题行，保持出现顺序。"""
    sections: list[tuple[str, str]] = []
    title: str | None = None
    body: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _HEADING.match(line)
        if match:
            if title is not None:
                sections.append((title, "\n".join(body).rstrip("\n")))
            title, body = match.group(1), []
        elif title is not None:
            body.append(line)
    if title is not None:
        sections.append((title, "\n".join(body).rstrip("\n")))
    return sections


def replace_section(text: str, title: str, body: str) -> str:
    """纯函数替换一个二级标题正文；标题不存在时追加。"""
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if _HEADING.match(line) and _HEADING.match(line).group(1) == title), None)
    clean = body.strip("\n")
    if start is None:
        suffix = "\n" if text.endswith("\n") else "\n\n"
        return text.rstrip("\n") + suffix + f"## {title}\n" + clean + "\n"
    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])), len(lines))
    out = lines[:start + 1] + (clean.split("\n") if clean else []) + lines[end:]
    return "\n".join(out).rstrip("\n") + "\n"


def parse_items(text: str) -> list[MemoryItem]:
    """解析列表条目及行尾 `id/src/t/imp/hits/lh` 等元数据。"""
    items: list[MemoryItem] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _ITEM.match(line)
        if not match:
            continue
        items.append(MemoryItem(match.group(1).strip(), parse_meta(match.group(2) or "")))
    return items


def render_item(item: MemoryItem) -> str:
    """稳定渲染列表条目；元数据以键名字典序输出，避免无意义的 diff。"""
    if not item.meta:
        return f"- {item.text}"
    meta = " | ".join(f"{key}: {item.meta[key]}" for key in sorted(item.meta))
    return f"- {item.text} <!-- {meta} -->"


def parse_user_items(text: str) -> dict[str, list[MemoryItem]]:
    """解析 USER 分节里“上一行注释 + 下一行指令”的 L1 元数据。"""
    out: dict[str, list[MemoryItem]] = {}
    for title, body in split_sections(text):
        pending: dict[str, str] = {}
        rows: list[MemoryItem] = []
        for line in body.split("\n"):
            comment = _META.search(line)
            if comment:
                pending = parse_meta(comment.group(1))
                continue
            match = _ITEM.match(line)
            if match:
                meta = dict(pending)
                meta.update(parse_meta(match.group(2) or ""))
                rows.append(MemoryItem(match.group(1).strip(), meta))
                pending = {}
        out[title] = rows
    return out


def parse_header(text: str) -> FileHeader:
    """解析 L3 文件头；缺字段时给稳定空值，便于空骨架参与索引。"""
    lines = text.replace("\r\n", "\n").split("\n")
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
    summary = next((line.removeprefix("> 摘要：").strip() for line in lines if line.startswith("> 摘要：")), "")
    raw_keywords = next((line.removeprefix("> 关键词：").strip() for line in lines if line.startswith("> 关键词：")), "")
    keywords = tuple(word for word in re.split(r"[、,，\s]+", raw_keywords) if word)
    meta = next((entry for entry in metadata_comments(text) if "layer" in entry or "visibility" in entry), {})
    return FileHeader(title, summary, keywords, meta)
