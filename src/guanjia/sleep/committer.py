"""P4-5 受控 staging 提交器。

只接受 P4-4 的 MEMORY/index staging。默认 dry-run；调用方必须显式选择 commit，
且本模块在锁内再次核验所有 manifest 输入 SHA 后才会通过 WorkspaceFiles 写入。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from guanjia.sleep.layers import over_limit_report
from guanjia.sleep.staging import ROOT, STAGING_ROOT, SnapshotChanged, snapshot_workspace
from guanjia.wsfiles import WorkspaceFiles
from guanjia.wslock import ws_lock

PLAN_FILES = frozenset({"MEMORY.md", "memory/index.md"})


class CommitRefused(RuntimeError):
    """不满足 fail-closed 条件，未调用任何 workspace 写入。"""


@dataclass(frozen=True)
class CommitResult:
    staging: Path
    files: tuple[str, ...]
    dry_run: bool
    over_limits: tuple[str, ...]


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            pass
    return False


def _read_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CommitRefused(f"{label} 无法读取：{e}") from e
    if not isinstance(data, dict):
        raise CommitRefused(f"{label} 必须是 JSON 对象")
    return data


def _stage_plan(directory: Path) -> dict[str, str]:
    plan_root = (directory / "plan").resolve()
    try:
        plan_root.relative_to(directory.resolve())
    except ValueError as e:
        raise CommitRefused("plan 路径越界") from e
    found: dict[str, str] = {}
    for path in plan_root.rglob("*") if plan_root.is_dir() else ():
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(plan_root).as_posix()
        except ValueError as e:
            raise CommitRefused("plan 文件越界") from e
        if rel not in PLAN_FILES:
            raise CommitRefused(f"staging 含不允许的计划文件：{rel}")
        found[rel] = resolved.read_text(encoding="utf-8").replace("\r\n", "\n")
    if set(found) != PLAN_FILES:
        raise CommitRefused("staging 必须且只能包含 MEMORY.md 与 memory/index.md")
    return found


def _manifest_inputs(manifest: Mapping[str, object]) -> dict[str, str]:
    if manifest.get("kind") != "p4-memory-index-dry-run" or manifest.get("inputs_stable") is not True:
        raise CommitRefused("manifest 不是稳定的 P4-4 index dry-run")
    rows = manifest.get("inputs")
    if not isinstance(rows, list) or not rows:
        raise CommitRefused("manifest 缺少输入依赖")
    inputs: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CommitRefused("manifest 输入格式错误")
        rel, digest = row.get("rel"), row.get("sha256")
        if not isinstance(rel, str) or not isinstance(digest, str) or len(digest) != 64 or rel in inputs:
            raise CommitRefused("manifest 输入 SHA 格式错误")
        inputs[rel] = digest
    return inputs


def _verify_inputs(fs: WorkspaceFiles, expected: Mapping[str, str]) -> dict[str, str]:
    """重读全部依赖；新增、删除或任一 SHA 不同都 fail-closed。"""
    snapshot = snapshot_workspace(fs.root)
    current = {rel: entry.sha256 for rel, entry in snapshot.files.items()}
    if set(current) != set(expected):
        added = sorted(set(current) - set(expected))
        removed = sorted(set(expected) - set(current))
        raise CommitRefused(f"manifest 依赖集合变化：新增 {added}；删除 {removed}")
    changed = sorted(rel for rel, digest in expected.items() if current[rel] != digest)
    if changed:
        raise CommitRefused("manifest 输入已变更：" + "、".join(changed))
    return {rel: entry.text for rel, entry in snapshot.files.items()}


def _verify_plan_budget(inputs: Mapping[str, str], plan: Mapping[str, str]) -> tuple[str, ...]:
    merged = dict(inputs)
    merged.update(plan)
    over = tuple(over_limit_report(merged))
    if over:
        raise CommitRefused("staged plan 超出预算：" + "；".join(over))
    return over


def commit_staging(
    directory: Path,
    fs: WorkspaceFiles,
    *,
    commit: bool = False,
    allowed_roots: Iterable[Path] = (ROOT / "data", ROOT / "tmp"),
    before_lock: Callable[[], None] | None = None,
) -> CommitResult:
    """预检 staging，dry-run 默认零写；commit 时锁内二次验 SHA 后只写两个白名单文件。"""
    directory = directory.resolve()
    if not _inside(directory, allowed_roots):
        raise CommitRefused("staging 必须位于项目 data/ 或 tmp/")
    if not directory.is_dir():
        raise CommitRefused("staging 目录不存在")
    manifest = _read_json(directory / "manifest.json", "manifest.json")
    expected = _manifest_inputs(manifest)
    plan = _stage_plan(directory)
    inputs = _verify_inputs(fs, expected)
    over = _verify_plan_budget(inputs, plan)
    result = CommitResult(directory, tuple(sorted(plan)), not commit, over)
    if not commit:
        return result

    if before_lock is not None:
        before_lock()
    with ws_lock(timeout=10, holder="p4-5", path=fs.lock_path):
        locked_inputs = _verify_inputs(fs, expected)
        _verify_plan_budget(locked_inputs, plan)
        for rel in sorted(plan):
            current = fs.read(rel)
            # write_unlocked 自己还会在替换前做最后一次 base_sha 比对。
            if current["text"] != plan[rel]:
                fs.write_unlocked(rel, plan[rel], current["sha"], actor="p4-5")
    return CommitResult(directory, tuple(sorted(plan)), False, over)
