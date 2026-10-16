"""P4-5 受控 committer：只用临时 workspace/staging，绝不读取线上 workspace。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p4_committer.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from guanjia.sleep.committer import CommitRefused, commit_staging  # noqa: E402
from guanjia.sleep.staging import snapshot_workspace, stage_index_snapshot  # noqa: E402
from guanjia.wsfiles import WorkspaceFiles  # noqa: E402

failed = 0


def check(condition: bool, name: str) -> None:
    global failed
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        failed += 1


def make_workspace(base: Path) -> Path:
    workspace = base / "workspace"
    (workspace / "memory" / "people").mkdir(parents=True)
    (workspace / "memory" / "topics").mkdir()
    (workspace / "SOUL.md").write_text("# 我\n", encoding="utf-8")
    (workspace / "IDENTITY.md").write_text("# 身份\n", encoding="utf-8")
    (workspace / "USER.md").write_text("# 用户档案\n\n## 主人\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text(
        "# 长期记忆\n## 关键事实\n## 悬而未决\n## 最近要点\n## 记忆目录\n旧目录\n", encoding="utf-8"
    )
    (workspace / "memory" / "people" / "主人.md").write_text(
        "# 主人\n> 摘要：人物档案。\n> 关键词：主人\n<!-- layer: L3 | source: fixture | visibility: family-default-on -->\n",
        encoding="utf-8",
    )
    return workspace


def make_stage(base: Path) -> tuple[Path, Path, Path]:
    workspace = make_workspace(base)
    staging_root = base / "data" / "sleep" / "staging"
    stage = stage_index_snapshot(snapshot_workspace(workspace), staging_root, run_id="approved").directory
    return workspace, staging_root, stage


class RecordingFiles(WorkspaceFiles):
    def __init__(self, root: Path, base: Path) -> None:
        super().__init__(root=root, history_dir=base / "history", lock_path=root / "guanjia" / "lock")
        self.writes: list[str] = []

    def write_unlocked(self, rel, text, base_sha, **kwargs):
        self.writes.append(rel)
        return super().write_unlocked(rel, text, base_sha, **kwargs)


def refused(fn, name: str) -> None:
    try:
        fn()
    except CommitRefused:
        check(True, name)
    else:
        check(False, name)


# ---- dry-run 与成功提交 ----
tmp = Path(tempfile.mkdtemp(prefix="gj-p4commit-"))
workspace, staging_root, stage = make_stage(tmp)
fs = RecordingFiles(workspace, tmp)
before = (workspace / "MEMORY.md").read_bytes()
result = commit_staging(stage, fs, allowed_roots=(tmp,))
check(result.dry_run and (workspace / "MEMORY.md").read_bytes() == before and not fs.writes, "dry-run performs zero workspace writes")
result = commit_staging(stage, fs, commit=True, allowed_roots=(tmp,))
check(not result.dry_run and (workspace / "memory" / "index.md").is_file(), "explicit commit writes approved plan")
check(set(fs.writes) == {"MEMORY.md", "memory/index.md"}, "commit only calls write_unlocked for the two allowed files")

# ---- fail-closed：非受信 staging、缺 manifest、超限、输入变更、锁前并发变更 ----
outside = tmp / "outside"
outside.mkdir()
refused(lambda: commit_staging(outside, fs, allowed_roots=(staging_root,)), "non-trusted staging is refused")

empty = staging_root / "missing-manifest"
empty.mkdir()
refused(lambda: commit_staging(empty, fs, allowed_roots=(tmp,)), "missing manifest is refused")

tmp_limit = Path(tempfile.mkdtemp(prefix="gj-p4limit-"))
limit_workspace, _, limit_stage = make_stage(tmp_limit)
limit_fs = RecordingFiles(limit_workspace, tmp_limit)
(limit_stage / "plan" / "MEMORY.md").write_text(
    "# 长期记忆\n## 关键事实\n" + "x" * 1000 + "\n## 悬而未决\n## 最近要点\n## 记忆目录\n", encoding="utf-8"
)
limit_before = (limit_workspace / "MEMORY.md").read_bytes()
refused(lambda: commit_staging(limit_stage, limit_fs, allowed_roots=(tmp_limit,)), "over-budget staged plan is refused")
check((limit_workspace / "MEMORY.md").read_bytes() == limit_before and not limit_fs.writes, "over-budget plan causes zero writes")

tmp_changed = Path(tempfile.mkdtemp(prefix="gj-p4changed-"))
changed_workspace, _, changed_stage = make_stage(tmp_changed)
changed_fs = RecordingFiles(changed_workspace, tmp_changed)
(changed_workspace / "USER.md").write_text("# 用户档案\n## 主人\n外部改动\n", encoding="utf-8")
refused(lambda: commit_staging(changed_stage, changed_fs, allowed_roots=(tmp_changed,)), "manifest input change is refused")
check(not changed_fs.writes, "pre-lock input change causes zero writes")

tmp_race = Path(tempfile.mkdtemp(prefix="gj-p4race-"))
race_workspace, _, race_stage = make_stage(tmp_race)
race_fs = RecordingFiles(race_workspace, tmp_race)
race_before = (race_workspace / "MEMORY.md").read_bytes()
refused(
    lambda: commit_staging(
        race_stage,
        race_fs,
        commit=True,
        allowed_roots=(tmp_race,),
        before_lock=lambda: (race_workspace / "USER.md").write_text("# 用户档案\n## 主人\n锁前并发修改\n", encoding="utf-8"),
    ),
    "change between preflight and lock is refused",
)
check((race_workspace / "MEMORY.md").read_bytes() == race_before and not race_fs.writes, "lock-time recheck causes zero writes")

print("ALL PASS test_p4_committer" if failed == 0 else f"{failed} FAILED test_p4_committer")
sys.exit(1 if failed else 0)
