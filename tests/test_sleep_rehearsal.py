"""P4 休眠演练 fixture：空闲门、独立 session、只 staging 和失败审计。"""

import json
import hashlib
import tempfile
from pathlib import Path

from guanjia.memory.maintenance import MaintenanceAction, MaintenancePlan
from guanjia.sleep.rehearsal import IDLE_SECONDS, SleepRehearsal
from guanjia.sleep.staging import SnapshotFile, WorkspaceSnapshot, stage_index_snapshot


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

    planned = []

    def maintenance_planner(snapshot_value, session_view, now):
        planned.append((snapshot_value, session_view, now))
        return MaintenancePlan((MaintenanceAction('rebuild_index', reason='fixture'),))

    runner = SleepRehearsal(
        workspace_snapshot=snapshot,
        staging_root=Path(tmp) / "staging",
        clock=lambda: 10_000,
        maintenance_planner=maintenance_planner,
        session_reader=lambda: {
            "connected": False,
            "limitation": "主线期间无新消息复核接口未接入",
            "token": "secret-token",
            "history": ["private conversation"],
            "internal_reasoning": "never persist this",
        },
        consolidator=lambda *_: {
            "accepted": True,
            "summary": "index updated",
            "messages": ["private transcript"],
            "reasoning": "private chain",
        },
    )
    skipped = runner.run(last_message_at=10_000 - IDLE_SECONDS + 1)
    check(skipped.status == "skipped", "recent main message prevents rehearsal")
    dry = runner.run(last_message_at=0, dry_run=True)
    check(dry.status == "dry_run" and dry.directory is None and dry.session_key.startswith("agent:main:sleep-rehearse-"),
          "dry run uses independent session without writes")
    done = runner.run(last_message_at=0, run_id="fixture")
    payload = json.loads((done.directory / "rehearsal.json").read_text(encoding="utf-8"))
    check(done.status == "rehearsed" and payload["commit_allowed"] is False and payload["sessions_reset_allowed"] is False,
          "rehearsal stages only and never commits or resets")
    check(payload["session_audit"] == {"connection_code": "disconnected", "recheck_available": False},
          "rehearsal audit uses only fixed session codes")
    encoded = json.dumps(payload, ensure_ascii=False)
    check(
        "secret-token" not in encoded and "private conversation" not in encoded and "private chain" not in encoded
        and "index updated" not in encoded and "主线期间无新消息复核接口未接入" not in encoded,
          "rehearsal log redacts sensitive session and consolidation details")
    check(payload["consolidation_audit"] == {"accepted": True, "outcome_code": "accepted"},
          "rehearsal log keeps only fixed consolidation codes")
    manifest = json.loads((done.directory / "manifest.json").read_text(encoding="utf-8"))
    check(
        manifest["maintenance_plan"]["actions"][0]["kind"] == "rebuild_index"
        and manifest["input_sha256"]["MEMORY.md"]
        and len(planned) == 2,
        "rehearsal plans maintenance and manifest audits input sha",
    )

    def fail_manifest(path, text):
        if path.name == "manifest.json":
            raise OSError("injected write failure")
        path.write_text(text, encoding="utf-8", newline="\n")

    failed_directory = Path(tmp) / "staging-failure" / "write-failure"
    try:
        stage_index_snapshot(snapshot(), failed_directory.parent, run_id=failed_directory.name, writer=fail_manifest)
    except OSError as error:
        check(str(error) == "injected write failure", "staging propagates write failure")
    else:
        raise SystemExit("FAIL staging write failure must be raised")
    check(not failed_directory.exists(), "staging removes partial new directory after write failure")

    existing_directory = Path(tmp) / "staging-failure" / "existing"
    existing_directory.mkdir(parents=True)
    (existing_directory / "keep.txt").write_text("keep", encoding="utf-8")
    try:
        stage_index_snapshot(snapshot(), existing_directory.parent, run_id="existing")
    except FileExistsError:
        pass
    else:
        raise SystemExit("FAIL existing staging directory must be rejected")
    check((existing_directory / "keep.txt").read_text(encoding="utf-8") == "keep", "existing staging is never deleted")

    raced_directory = Path(tmp) / "staging-failure" / "raced"

    def race_create(path):
        path.mkdir(parents=True)
        (path / "competitor.txt").write_text("keep", encoding="utf-8")
        raise FileExistsError("created by another runner")

    try:
        stage_index_snapshot(
            snapshot(),
            raced_directory.parent,
            run_id=raced_directory.name,
            create_directory=race_create,
        )
    except FileExistsError as error:
        check(str(error) == "created by another runner", "concurrent staging creation is rejected")
    else:
        raise SystemExit("FAIL concurrent staging creation must be raised")
    check((raced_directory / "competitor.txt").read_text(encoding="utf-8") == "keep", "concurrent staging is never deleted")

print("ALL PASS test_sleep_rehearsal")
