"""分层记忆的字符预算；唯一来源见 2026-10-13-memory-design.md。"""

from __future__ import annotations

SOUL = 1500
IDENTITY = 150
AGENTS = 2500
USER_TOTAL = 4000
USER_OWNER = 2400
USER_COMMON = 400
USER_FAMILY = 600
MEM_FACTS = 900
MEM_OPEN = 700
MEM_RECENT = 1600
MEM_INDEX = 1200
MEMORY_TOTAL = MEM_FACTS + MEM_OPEN + MEM_RECENT + MEM_INDEX + 200
OPEN_LOOPS_MAX = 8
RECENT_SEGMENTS = 3
LONG_FILE = 6000
DAILY = 12000
INDEX_FULL = 8000
ITEM_MAX = 120
PREFETCH = 800
RELEVANT_INPUT = 12000

USER_SECTIONS = {"主人": USER_OWNER, "通用": USER_COMMON}
MEMORY_SECTIONS = {
    "关键事实": MEM_FACTS,
    "悬而未决": MEM_OPEN,
    "最近要点": MEM_RECENT,
    "记忆目录": MEM_INDEX,
}


def file_limit(rel: str) -> int | None:
    """返回文件总预算；未知文件不强加预算。"""
    rel = rel.replace("\\", "/")
    if rel == "SOUL.md":
        return SOUL
    if rel == "IDENTITY.md":
        return IDENTITY
    if rel == "AGENTS.md":
        return AGENTS
    if rel == "USER.md":
        return USER_TOTAL
    if rel == "MEMORY.md":
        return MEMORY_TOTAL
    if rel == "memory/index.md":
        return INDEX_FULL
    if rel.startswith(("memory/people/", "memory/topics/")):
        return LONG_FILE
    if rel.startswith("memory/") and rel.endswith(".md") and rel[7:17].count("-") == 2:
        return DAILY
    return None
