"""Windows 应用索引：只收集、匹配和核实，不启动程序。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # 非 Windows / 最小环境仍可测英文和手工别名
    Style = None
    lazy_pinyin = None


MatchKind = Literal["unique", "ambiguous", "not_found"]
VerifyKind = Literal["verified", "not_verified"]


@dataclass(frozen=True)
class AppCandidate:
    """一个可显示、可由未来启动器处理的应用候选；target 可为空。"""

    name: str
    source: str
    target: str = ""
    aliases: tuple[str, ...] = ()
    process_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchResult:
    """查询结果；只有 unique 才允许未来接线层继续处理。"""

    kind: MatchKind
    candidates: tuple[AppCandidate, ...] = ()


@dataclass(frozen=True)
class VerifyResult:
    """拉起后的纯核实结论，不创建或终止任何进程。"""

    kind: VerifyKind
    reason: str


def _forms(value: str) -> set[str]:
    """中文/英文名称 → 原形、拼音全拼和拼音首字母。"""
    text = value.strip().casefold()
    compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    forms = {compact} if compact else set()
    if lazy_pinyin and any("\u4e00" <= char <= "\u9fff" for char in text):
        syllables = lazy_pinyin(text, style=Style.NORMAL)
        forms.add("".join(syllables))
        forms.add("".join(item[0] for item in syllables if item))
    return forms


def _dedupe(candidates: Iterable[AppCandidate]) -> list[AppCandidate]:
    """同一来源/目标的重复项合并别名，保留确定顺序。"""
    merged: dict[tuple[str, str], AppCandidate] = {}
    for candidate in candidates:
        key = (candidate.source, candidate.target.casefold() or candidate.name.casefold())
        previous = merged.get(key)
        if previous is None:
            merged[key] = candidate
            continue
        aliases = tuple(dict.fromkeys((*previous.aliases, *candidate.aliases)))
        processes = tuple(dict.fromkeys((*previous.process_names, *candidate.process_names)))
        merged[key] = AppCandidate(previous.name, previous.source, previous.target, aliases, processes)
    return list(merged.values())


class AppIndex:
    """独立索引；所有 Windows 读取器都可替换为 fixture。"""

    def __init__(self, candidates: Iterable[AppCandidate] = ()) -> None:
        self.candidates = tuple(_dedupe(candidates))

    @classmethod
    def collect_windows(
        cls,
        *,
        start_menu: Callable[[], Iterable[AppCandidate]] | None = None,
        app_paths: Callable[[], Iterable[AppCandidate]] | None = None,
        start_apps: Callable[[], Iterable[AppCandidate]] | None = None,
        manual_aliases: dict[str, Iterable[str]] | None = None,
    ) -> AppIndex:
        """从三个受限 Windows 数据源收集；测试可传 fixture，绝不扫描整盘。"""
        readers = (start_menu or read_start_menu, app_paths or read_app_paths, start_apps or read_start_apps)
        candidates = [candidate for reader in readers for candidate in reader()]
        aliases = manual_aliases or {}
        augmented = []
        for candidate in candidates:
            extra = tuple(aliases.get(candidate.name.casefold(), ()))
            augmented.append(AppCandidate(candidate.name, candidate.source, candidate.target,
                                          tuple(dict.fromkeys((*candidate.aliases, *extra))), candidate.process_names))
        return cls(augmented)

    def match(self, query: str) -> MatchResult:
        """先精确命中，再做唯一包含匹配；多条候选永远返回 ambiguous。"""
        query_forms = _forms(query)
        if not query_forms:
            return MatchResult("not_found")
        exact, partial = [], []
        for candidate in self.candidates:
            names = (candidate.name, *candidate.aliases)
            forms = set().union(*(_forms(name) for name in names))
            if forms & query_forms:
                exact.append(candidate)
            elif any(query in form or form in query for query in query_forms for form in forms):
                partial.append(candidate)
        hits = exact or partial
        if not hits:
            return MatchResult("not_found")
        return MatchResult("unique" if len(hits) == 1 else "ambiguous", tuple(hits))

    @staticmethod
    def verify(candidate: AppCandidate, *, process_names: Iterable[str] = (), window_titles: Iterable[str] = ()) -> VerifyResult:
        """未来启动器可注入进程/窗口快照核实；没有副作用。"""
        processes = {name.casefold() for name in process_names}
        expected = {name.casefold() for name in candidate.process_names}
        if expected & processes:
            return VerifyResult("verified", "process")
        names = _forms(candidate.name)
        if any(any(form in title.casefold() for form in names) for title in window_titles):
            return VerifyResult("verified", "window")
        return VerifyResult("not_verified", "not_found")


def index_from_apps_json(path: str | Path) -> AppIndex:
    """把受控 apps.json 映射建成索引；回退匹配绝不扩大可启动应用集合。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    candidates = []
    for key, item in (raw.get("apps") or {}).items():
        if not isinstance(item, dict):
            continue
        candidates.append(AppCandidate(
            name=str(key),
            source="apps_json",
            target=str(item.get("target") or ""),
            aliases=tuple(str(name) for name in item.get("names", []) if isinstance(name, str)),
            process_names=(Path(str(item.get("target") or "")).name,) if str(item.get("target") or "").lower().endswith(".exe") else (),
        ))
    return AppIndex(candidates)


def read_start_menu() -> Iterable[AppCandidate]:
    """仅遍历两个已知开始菜单目录的 .lnk 文件；不解析快捷方式目标。"""
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for shortcut in root.rglob("*.lnk"):
            yield AppCandidate(shortcut.stem, "start_menu", str(shortcut))


def read_app_paths() -> Iterable[AppCandidate]:
    """读取 HKCU/HKLM App Paths，不枚举文件系统。"""
    try:
        import winreg
    except ImportError:
        return
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
        except OSError:
            continue
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
                index += 1
                with winreg.OpenKey(key, name) as app_key:
                    target = str(winreg.QueryValue(app_key, None))
                yield AppCandidate(Path(name).stem, "app_paths", target, process_names=(Path(target).name,))
            except OSError:
                break


def read_start_apps() -> Iterable[AppCandidate]:
    """调用 Get-StartApps；其 AUMID 仅作为未来 Shell 启动的标识，不执行应用。"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
        )
        rows = json.loads(result.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    for row in rows if isinstance(rows, list) else [rows]:
        if isinstance(row, dict) and row.get("Name"):
            yield AppCandidate(str(row["Name"]), "start_apps", str(row.get("AppID") or ""))


def main() -> None:
    """给固定 PowerShell 启动器的 JSON 查询入口；不收集系统来源，也不启动程序。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--apps-json", required=True)
    parser.add_argument("--query", required=True)
    args = parser.parse_args()
    result = index_from_apps_json(args.apps_json).match(args.query)
    print(json.dumps({
        "kind": result.kind,
        "candidates": [{"key": item.name, "target": item.target} for item in result.candidates],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
