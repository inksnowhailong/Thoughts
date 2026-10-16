"""L2/L3 目录的纯函数重建；调用方决定是否把结果写入工作区。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from guanjia.sleep import caps
from guanjia.sleep.items import parse_header, parse_items, replace_section, split_sections


def _long_files(files: Mapping[str, str]) -> list[tuple[str, str]]:
    return [(rel, files[rel]) for rel in sorted(files) if rel.startswith(("memory/people/", "memory/topics/")) and rel.endswith(".md")]


def _label(rel: str, text: str) -> str:
    header = parse_header(text)
    title = header.title or rel.rsplit("/", 1)[-1][:-3]
    return f"{title}（{len(parse_items(text))} 条）"


def render_indexes(files: Mapping[str, str], *, daily_notes: Sequence[str] = (), timeline_lines: int = 0) -> tuple[str, str]:
    """返回 `(MEMORY.md 的目录正文, memory/index.md 全文)`，输入只读。"""
    people: list[tuple[str, str]] = []
    topics: list[tuple[str, str]] = []
    for rel, text in _long_files(files):
        (people if rel.startswith("memory/people/") else topics).append((rel, text))

    summary = ["（自动生成。用 memory_search 查，或 memory_get 读整份。）"]
    if people:
        summary.append("- 人物：" + " · ".join(_label(rel, text) for rel, text in people))
    if topics:
        summary.append("- 主题：" + " · ".join(_label(rel, text) for rel, text in topics))
    if daily_notes or timeline_lines:
        summary.append(f"- 日记：{len(daily_notes)} 天 · 时间线 {timeline_lines} 行")
    l2 = "\n".join(summary)[:caps.MEM_INDEX]

    full = ["# 记忆全目录", "<!-- layer: L3 | source: generated-index | visibility: retrieval-only -->", ""]
    for rel, text in people + topics:
        header = parse_header(text)
        full.append(f"## {rel}")
        full.append(f"> 摘要：{header.summary or '未填写'}")
        full.append("> 关键词：" + ("、".join(header.keywords) or "未填写"))
        full.append(f"> {len(parse_items(text))} 条")
        sections = [title for title, _ in split_sections(text)]
        if sections:
            full.append("- 小节：" + "、".join(sections))
        full.append("")
    return l2, "\n".join(full).rstrip("\n")[:caps.INDEX_FULL] + "\n"


def rebuild_indexes_dry_run(files: Mapping[str, str], *, daily_notes: Sequence[str] = (), timeline_lines: int = 0) -> dict[str, str]:
    """纯函数生成将要写入的两个文件；绝不读取或写入工作区。"""
    if "MEMORY.md" not in files:
        raise ValueError("缺少 MEMORY.md")
    l2, full = render_indexes(files, daily_notes=daily_notes, timeline_lines=timeline_lines)
    return {
        "MEMORY.md": replace_section(files["MEMORY.md"], "记忆目录", l2),
        "memory/index.md": full,
    }
