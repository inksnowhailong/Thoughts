"""P4-4 安全 staging runner：只用临时 workspace，绝不读取线上 workspace。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p4_staging.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.sleep.staging import SnapshotChanged, snapshot_changes, snapshot_workspace, stage_index_snapshot  # noqa: E402

failed = 0


def check(condition: bool, name: str) -> None:
    global failed
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        failed += 1


tmp = Path(tempfile.mkdtemp(prefix="gj-p4stage-"))
workspace = tmp / "workspace"
staging = tmp / "data" / "sleep" / "staging"
(workspace / "memory" / "people").mkdir(parents=True)
(workspace / "memory" / "topics").mkdir()

(workspace / "SOUL.md").write_text("# 我\n", encoding="utf-8")
(workspace / "IDENTITY.md").write_text("# 身份\n", encoding="utf-8")
(workspace / "USER.md").write_text("# 用户档案\n\n## 主人\n", encoding="utf-8")
memory = """# 长期记忆
## 关键事实
- 家里有音箱。 <!-- id: k1 | src: panel | imp: 6 -->
## 悬而未决
## 最近要点
## 记忆目录
旧目录
"""
(workspace / "MEMORY.md").write_text(memory, encoding="utf-8")
(workspace / "memory" / "people" / "主人.md").write_text(
    "# 主人\n> 摘要：人物档案。\n> 关键词：主人、你\n<!-- layer: L3 | source: fixture | visibility: family-default-on -->\n\n## 事实\n",
    encoding="utf-8",
)
(workspace / "memory" / "topics" / "设备.md").write_text(
    "# 设备\n> 摘要：家里设备。\n> 关键词：音箱、设备\n<!-- layer: L3 | source: fixture | visibility: family-default-on -->\n\n## 设备\n",
    encoding="utf-8",
)
(workspace / "memory" / "timeline.md").write_text("2026-10-14 | 主人 | 决定 | 骨架 | src: panel\n", encoding="utf-8")

before = (workspace / "MEMORY.md").read_bytes()
snapshot = snapshot_workspace(workspace)
result = stage_index_snapshot(snapshot, staging, run_id="fixture-ok")

check((workspace / "MEMORY.md").read_bytes() == before, "runner never writes workspace input")
check((result.directory / "plan" / "MEMORY.md").is_file() and (result.directory / "plan" / "memory" / "index.md").is_file(),
      "staging contains MEMORY and index plan only")
check("旧目录" not in (result.directory / "plan" / "MEMORY.md").read_text(encoding="utf-8"), "planned MEMORY replaces only L2 directory")
manifest = json.loads((result.directory / "manifest.json").read_text(encoding="utf-8"))
dry_run = json.loads((result.directory / "dry-run.json").read_text(encoding="utf-8"))
check(manifest["inputs_stable"] is True and any(row["rel"] == "MEMORY.md" and row["sha256"] for row in manifest["inputs"]),
      "manifest records input SHA and stability")
check("MEMORY.md" in dry_run["changed"] and "memory/index.md" in dry_run["changed"], "dry-run lists planned changes")
check("P4-5" in (result.directory / "README.md").read_text(encoding="utf-8"), "staging documents manual P4-5 handoff")
check(manifest["input_sha256_pre"] == manifest["input_sha256_post"] and manifest["verified_at"],
      "manifest proves pre and post input SHA match")

changed = snapshot_workspace(workspace)
(workspace / "MEMORY.md").write_text(memory + "\n# 外部修改\n", encoding="utf-8")
check(snapshot_changes(changed) == ["~ MEMORY.md"], "snapshot detects an input change before staging")
try:
    stage_index_snapshot(changed, staging, run_id="fixture-changed")
except SnapshotChanged:
    check(True, "changed input refuses staging")
else:
    check(False, "changed input refuses staging")

postcheck_snapshot = snapshot_workspace(workspace)
postcheck_directory = staging / "fixture-postcheck"

def mutate_workspace_after_write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    if path.name == "budget.json":
        (workspace / "MEMORY.md").write_text(memory + "\n# 并发修改\n", encoding="utf-8")

try:
    stage_index_snapshot(postcheck_snapshot, staging, run_id="fixture-postcheck", writer=mutate_workspace_after_write)
except SnapshotChanged:
    check(True, "post-write input change refuses staging")
else:
    check(False, "post-write input change refuses staging")
check(not postcheck_directory.exists(), "post-write change removes this call's partial staging")

final_snapshot = snapshot_workspace(workspace)
final_directory = staging / "fixture-final-manifest"
manifest_writes = 0

def mutate_after_final_manifest(path: Path, text: str) -> None:
    global manifest_writes
    path.write_text(text, encoding="utf-8", newline="\n")
    if path.name == "manifest.json":
        manifest_writes += 1
        if manifest_writes == 2:
            (workspace / "MEMORY.md").write_text(memory + "\n# 最终写入竞争\n", encoding="utf-8")

try:
    stage_index_snapshot(final_snapshot, staging, run_id="fixture-final-manifest", writer=mutate_after_final_manifest)
except SnapshotChanged:
    check(True, "final manifest write change refuses staging")
else:
    check(False, "final manifest write change refuses staging")
check(not final_directory.exists(), "final manifest race removes partial staging")

try:
    stage_index_snapshot(snapshot_workspace(workspace), staging, run_id="fixture-malicious", maintenance_plan=object())
except ValueError as error:
    check(str(error) == "maintenance_plan must be a MaintenancePlan", "manifest rejects reflective maintenance objects")
else:
    check(False, "manifest rejects reflective maintenance objects")

print("ALL PASS test_p4_staging" if failed == 0 else f"{failed} FAILED test_p4_staging")
sys.exit(1 if failed else 0)
