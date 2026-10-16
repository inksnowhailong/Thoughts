"""分层记忆的预算报告；只分析文本，不做升降或写入。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from guanjia.sleep import caps
from guanjia.sleep.items import split_sections


@dataclass(frozen=True)
class BudgetEntry:
    """一个文件或分节的预算占用。"""

    rel: str
    section: str | None
    chars: int
    limit: int | None

    @property
    def over(self) -> int:
        return max(0, self.chars - (self.limit or self.chars))


def budget_report(files: Mapping[str, str]) -> list[BudgetEntry]:
    """为 L0/L1/L2/L3 文件生成稳定的预算报告，不修改输入。"""
    report: list[BudgetEntry] = []
    for rel in sorted(files):
        text = files[rel]
        report.append(BudgetEntry(rel, None, len(text), caps.file_limit(rel)))
        if rel == "USER.md":
            for title, body in split_sections(text):
                limit = caps.USER_SECTIONS.get(title, caps.USER_FAMILY)
                report.append(BudgetEntry(rel, title, len(body), limit))
        elif rel == "MEMORY.md":
            for title, body in split_sections(text):
                report.append(BudgetEntry(rel, title, len(body), caps.MEMORY_SECTIONS.get(title)))
    return report


def over_limit_report(files: Mapping[str, str]) -> list[str]:
    """返回所有超限项的稳定中文报告；空列表即没有已知预算超限。"""
    out = []
    for entry in budget_report(files):
        if entry.limit is not None and entry.chars > entry.limit:
            where = f"{entry.rel} / {entry.section}" if entry.section else entry.rel
            out.append(f"{where}: {entry.chars}/{entry.limit}（超 {entry.over}）")
    return out
