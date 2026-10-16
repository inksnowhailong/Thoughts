"""安全的分层记忆 staging runner。

只读取工作区快照，把 P4-3 的目录重建计划写进 data/tmp 下的 staging。
本模块没有提交路径，绝不写回 workspace。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from guanjia.sleep.index import rebuild_indexes_dry_run
from guanjia.sleep.layers import budget_report, over_limit_report

ROOT = Path(__file__).resolve().parents[3]
WORKSPACE = Path.home() / ".openclaw" / "workspace"
STAGING_ROOT = ROOT / "data" / "sleep" / "staging"
_DAILY = re.compile(r"^memory/\d{4}-\d{2}-\d{2}\.md$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class SnapshotChanged(RuntimeError):
    """输入在快照计算期间变化，拒绝产生可能混合版本的 staging。"""


@dataclass(frozen=True)
class SnapshotFile:
    rel: str
    text: str
    sha256: str
    bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: Path
    captured_at: str
    files: dict[str, SnapshotFile]


@dataclass(frozen=True)
class StageResult:
    directory: Path
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    over_limits: tuple[str, ...]


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _paths(root: Path) -> list[Path]:
    """列出索引计算真正依赖的文件，避免读取无关工作区内容。"""
    paths = [root / name for name in ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md") if (root / name).is_file()]
    memory = root / "memory"
    if memory.is_dir():
        paths.extend(path for path in memory.glob("*.md") if path.is_file())
        for subdir in ("people", "topics"):
            child = memory / subdir
            if child.is_dir():
                paths.extend(path for path in child.glob("*.md") if path.is_file())
    return sorted({path.resolve() for path in paths})


def snapshot_workspace(root: Path = WORKSPACE) -> WorkspaceSnapshot:
    """读取一份 UTF-8 工作区快照；不创建锁文件，也不写 workspace。"""
    root = root.resolve()
    files: dict[str, SnapshotFile] = {}
    for path in _paths(root):
        raw = path.read_bytes()
        stat = path.stat()
        rel = _rel(root, path)
        files[rel] = SnapshotFile(rel, raw.decode("utf-8-sig").replace("\r\n", "\n"),
                                  hashlib.sha256(raw).hexdigest(), len(raw), stat.st_mtime_ns)
    if "MEMORY.md" not in files:
        raise ValueError(f"工作区缺少 MEMORY.md：{root}")
    return WorkspaceSnapshot(root, time.strftime("%Y-%m-%dT%H:%M:%S%z"), files)


def snapshot_changes(snapshot: WorkspaceSnapshot) -> list[str]:
    """复扫依赖文件并比 SHA；空列表才说明 staging 可对应同一份输入。"""
    current = {_rel(snapshot.root, path): path for path in _paths(snapshot.root)}
    changed = [f"+ {rel}" for rel in sorted(current.keys() - snapshot.files.keys())]
    changed += [f"- {rel}" for rel in sorted(snapshot.files.keys() - current.keys())]
    for rel in sorted(current.keys() & snapshot.files.keys()):
        if hashlib.sha256(current[rel].read_bytes()).hexdigest() != snapshot.files[rel].sha256:
            changed.append(f"~ {rel}")
    return changed


def _timeline_lines(files: Mapping[str, str]) -> int:
    return len([line for line in files.get("memory/timeline.md", "").splitlines() if line.strip()])


def _daily_notes(files: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted(rel for rel in files if _DAILY.fullmatch(rel)))


def stage_index_snapshot(snapshot: WorkspaceSnapshot, staging_root: Path = STAGING_ROOT, *, run_id: str | None = None) -> StageResult:
    """计算并落 staging；在落盘前复核输入，任何变化都拒绝输出。"""
    texts = {rel: entry.text for rel, entry in snapshot.files.items()}
    plan = rebuild_indexes_dry_run(texts, daily_notes=_daily_notes(texts), timeline_lines=_timeline_lines(texts))
    entries = budget_report(texts)
    over = over_limit_report(texts)
    changes = snapshot_changes(snapshot)
    if changes:
        raise SnapshotChanged("输入已变化：" + "；".join(changes))

    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-index"
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("staging 名称只能含字母、数字、点、短横线和下划线")
    directory = staging_root.resolve() / run_id
    if directory.exists():
        raise FileExistsError(f"staging 已存在：{directory}")
    directory.mkdir(parents=True)

    changed = tuple(sorted(rel for rel, text in plan.items() if texts.get(rel) != text))
    unchanged = tuple(sorted(rel for rel, text in plan.items() if texts.get(rel) == text))
    manifest = {
        "kind": "p4-memory-index-dry-run",
        "captured_at": snapshot.captured_at,
        "workspace": str(snapshot.root),
        "inputs_stable": True,
        "inputs": [{"rel": entry.rel, "sha256": entry.sha256, "bytes": entry.bytes, "mtime_ns": entry.mtime_ns}
                   for entry in (snapshot.files[rel] for rel in sorted(snapshot.files))],
    }
    dry_run = {
        "changed": list(changed),
        "unchanged": list(unchanged),
        "over_limits": over,
        "commit": "禁止：本 staging 仅供人工演练；P4-5 才能在锁和 SHA 复核下提交。",
    }
    (directory / "plan").mkdir()
    for rel, text in plan.items():
        target = directory / "plan" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "budget.json").write_text(json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "dry-run.json").write_text(json.dumps(dry_run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "README.md").write_text(
        "# P4 分层记忆演练 staging\n\n"
        "这是一份只读快照计算结果，不会写回 workspace。\n\n"
        "P4-5 前由人工核对 `manifest.json` 的输入 SHA、`budget.json` 的超限项和 `plan/` 内容；"
        "确认后仍必须重新读取工作区、拿写锁并再次比对 SHA，不能直接复制本目录覆盖文件。\n",
        encoding="utf-8",
    )
    return StageResult(directory, changed, unchanged, tuple(over))
