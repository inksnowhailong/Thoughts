"""安全的分层记忆 staging runner。

只读取工作区快照，把 P4-3 的目录重建计划写进 data/tmp 下的 staging。
本模块没有提交路径，绝不写回 workspace。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from guanjia.memory.maintenance import MaintenancePlan
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


def snapshot_workspace(
    root: Path = WORKSPACE,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> WorkspaceSnapshot:
    """读取一份 UTF-8 工作区快照；不创建锁文件，也不写 workspace。"""
    root = root.resolve()
    captured_at = clock()
    if captured_at.tzinfo is None or captured_at.utcoffset() != timezone.utc.utcoffset(captured_at):
        raise ValueError('clock must return an aware UTC datetime')
    files: dict[str, SnapshotFile] = {}
    for path in _paths(root):
        raw = path.read_bytes()
        stat = path.stat()
        rel = _rel(root, path)
        files[rel] = SnapshotFile(rel, raw.decode("utf-8-sig").replace("\r\n", "\n"),
                                  hashlib.sha256(raw).hexdigest(), len(raw), stat.st_mtime_ns)
    if "MEMORY.md" not in files:
        raise ValueError(f"工作区缺少 MEMORY.md：{root}")
    return WorkspaceSnapshot(root, captured_at.isoformat(), files)


def snapshot_changes(snapshot: WorkspaceSnapshot) -> list[str]:
    """复扫依赖文件并比 SHA；空列表才说明 staging 可对应同一份输入。"""
    current = {_rel(snapshot.root, path): path for path in _paths(snapshot.root)}
    changed = [f"+ {rel}" for rel in sorted(current.keys() - snapshot.files.keys())]
    changed += [f"- {rel}" for rel in sorted(snapshot.files.keys() - current.keys())]
    for rel in sorted(current.keys() & snapshot.files.keys()):
        if hashlib.sha256(current[rel].read_bytes()).hexdigest() != snapshot.files[rel].sha256:
            changed.append(f"~ {rel}")
    return changed


def _current_hashes(snapshot: WorkspaceSnapshot) -> dict[str, str]:
    """Read the live input set again; callers must first reject topology/content changes."""
    return {
        _rel(snapshot.root, path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _paths(snapshot.root)
    }


def _timeline_lines(files: Mapping[str, str]) -> int:
    return len([line for line in files.get("memory/timeline.md", "").splitlines() if line.strip()])


def _daily_notes(files: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted(rel for rel in files if _DAILY.fullmatch(rel)))


def _plan_manifest(plan: MaintenancePlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    if not isinstance(plan, MaintenancePlan):
        raise ValueError('maintenance_plan must be a MaintenancePlan')
    return {
        'actions': [
            {
                'kind': action.kind,
                'record_ids': list(action.record_ids),
                'retention': action.retention.value if action.retention else None,
                'reason_code': f'maintenance.{action.kind}',
            }
            for action in plan.actions
        ],
        'budget_before': plan.budget_before,
        'budget_after': plan.budget_after,
        'rejection_count': len(plan.rejections),
    }


def _reference_manifest(values: tuple[str, ...]) -> dict[str, object]:
    return {
        'count': len(values),
        'hashes': [hashlib.sha256(value.encode('utf-8')).hexdigest() for value in values],
    }


def _budget_manifest(values: Mapping[str, int] | None) -> dict[str, int]:
    values = values or {}
    result: dict[str, int] = {}
    for key in ('records_before', 'records_after'):
        value = values.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError(f'{key} must be a non-negative integer')
        result[key] = value
    return result


def stage_index_snapshot(
    snapshot: WorkspaceSnapshot,
    staging_root: Path = STAGING_ROOT,
    *,
    run_id: str | None = None,
    maintenance_plan: MaintenancePlan | None = None,
    conflicts: tuple[str, ...] = (),
    media_references: tuple[str, ...] = (),
    budget_changes: Mapping[str, int] | None = None,
    rejection_reasons: tuple[str, ...] = (),
    writer: Callable[[Path, str], None] | None = None,
    create_directory: Callable[[Path], None] | None = None,
) -> StageResult:
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
    created_by_this_call = False
    try:
        (create_directory or _create_directory)(directory)
        created_by_this_call = True
        changed = tuple(sorted(rel for rel, text in plan.items() if texts.get(rel) != text))
        unchanged = tuple(sorted(rel for rel, text in plan.items() if texts.get(rel) == text))
        plan_manifest = _plan_manifest(maintenance_plan)
        if maintenance_plan is not None and budget_changes is None:
            budget_changes = {'records_before': maintenance_plan.budget_before, 'records_after': maintenance_plan.budget_after}
        input_sha256_pre = {rel: snapshot.files[rel].sha256 for rel in sorted(snapshot.files)}
        manifest = {
            "kind": "p4-memory-index-dry-run",
            "captured_at": snapshot.captured_at,
            "workspace": str(snapshot.root),
            "inputs_stable": False,
            "inputs": [{"rel": entry.rel, "sha256": entry.sha256, "bytes": entry.bytes, "mtime_ns": entry.mtime_ns}
                       for entry in (snapshot.files[rel] for rel in sorted(snapshot.files))],
            "input_sha256": input_sha256_pre,
            "input_sha256_pre": input_sha256_pre,
            "input_sha256_post": None,
            "verified_at": None,
            "maintenance_plan": plan_manifest,
            "conflicts": _reference_manifest(conflicts),
            "media_references": _reference_manifest(media_references),
            "budget_changes": _budget_manifest(budget_changes),
            "rejection_codes": {'count': len(rejection_reasons) + (len(maintenance_plan.rejections) if maintenance_plan else 0)},
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
            _write(target, text, writer)
        _write(directory / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", writer)
        _write(directory / "budget.json", json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, indent=2) + "\n", writer)
        _write(directory / "dry-run.json", json.dumps(dry_run, ensure_ascii=False, indent=2) + "\n", writer)
        _write(
            directory / "README.md",
            "# P4 分层记忆演练 staging\n\n"
            "这是一份只读快照计算结果，不会写回 workspace。\n\n"
            "P4-5 前由人工核对 `manifest.json` 的输入 SHA、`budget.json` 的超限项和 `plan/` 内容；"
            "确认后仍必须重新读取工作区、拿写锁并再次比对 SHA，不能直接复制本目录覆盖文件。\n",
            writer,
        )
        post_changes = snapshot_changes(snapshot)
        if post_changes:
            raise SnapshotChanged("输入在 staging 写入期间变化：" + "；".join(post_changes))
        manifest["input_sha256_post"] = _current_hashes(snapshot)
        manifest["inputs_stable"] = manifest["input_sha256_pre"] == manifest["input_sha256_post"]
        manifest["verified_at"] = datetime.now(timezone.utc).isoformat()
        _write(directory / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", writer)
        final_changes = snapshot_changes(snapshot)
        if final_changes:
            raise SnapshotChanged("最终 manifest 写入期间输入已变化：" + "；".join(final_changes))
    except Exception:
        if created_by_this_call and directory.exists():
            shutil.rmtree(directory)
        raise
    return StageResult(directory, changed, unchanged, tuple(over))


def _write(path: Path, text: str, writer: Callable[[Path, str], None] | None) -> None:
    if writer is not None:
        writer(path, text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")


def _create_directory(path: Path) -> None:
    path.mkdir(parents=True)
