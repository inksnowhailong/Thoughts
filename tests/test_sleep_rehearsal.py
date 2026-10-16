"""P4 休眠演练 fixture：空闲门、独立 session、只 staging 和失败审计。"""

import json
import hashlib
import tempfile
from pathlib import Path

from guanjia.sleep.rehearsal import IDLE_SECONDS, SleepRehearsal
from guanjia.sleep.staging import SnapshotFile, WorkspaceSnapshot


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "workspace"
    (root / "memory").mkdir(parents=True)
    (root / "MEMORY.md").write_text("# MEMORY\n## 记忆目录\n旧\n", encoding="utf-8")
    (root / "memory" / "timeline.md").write_text("- 一条\n", encoding="utf-8")

    def snapshot():
        files = {}
        for rel in ("MEMORY.md", "memory/timeline.md"):
            p = root / rel
            raw = p.read_bytes()
            files[rel] = SnapshotFile(rel, raw.decode(), hashlib.sha256(raw).hexdigest(), len(raw), p.stat().st_mtime_ns)
        return WorkspaceSnapshot(root, "fixture", files)

    runner = SleepRehearsal(workspace_snapshot=snapshot, staging_root=Path(tmp) / "staging", clock=lambda: 10_000)
    skipped = runner.run(last_message_at=10_000 - IDLE_SECONDS + 1)
    check(skipped.status == "skipped", "recent main message prevents rehearsal")
    dry = runner.run(last_message_at=0, dry_run=True)
    check(dry.status == "dry_run" and dry.directory is None and dry.session_key.startswith("agent:main:sleep-rehearse-"),
          "dry run uses independent session without writes")
    done = runner.run(last_message_at=0, run_id="fixture")
    payload = json.loads((done.directory / "rehearsal.json").read_text(encoding="utf-8"))
    check(done.status == "rehearsed" and payload["commit"] == "禁止" and payload["sessions_reset"] == "禁止",
          "rehearsal stages only and never commits or resets")
    check("主线期间无新消息复核接口未接入" in str(payload["session_snapshot"]), "missing live recheck is audited")

print("ALL PASS test_sleep_rehearsal")
