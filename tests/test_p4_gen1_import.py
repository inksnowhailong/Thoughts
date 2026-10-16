"""P4-6 初代画像暂存：全部用 fixture，绝不读取真实画像或 workspace。"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.sleep.gen1_import import CandidateInputError, load_candidate_input, prepare_candidates, stage_candidates  # noqa: E402
from guanjia.sleep.__main__ import main  # noqa: E402

failed = 0


def check(condition: bool, name: str) -> None:
    global failed
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        failed += 1


tmp = Path(tempfile.mkdtemp(prefix="gj-p46-"))
workspace = tmp / "workspace"
workspace.mkdir()
(workspace / "MEMORY.md").write_text("真实工作区哨兵\n", encoding="utf-8")
input_path = tmp / "fixture-candidates.json"
input_path.write_text(json.dumps({"candidates": [
    {
        "content": "主人偏好一句话说完。",
        "target_layer": "L1",
        "target": "core.user.owner",
        "source": "gen1:fixture/user-portrait.md",
        "topic": "沟通偏好",
        "importance": 6,
    },
    {
        "content": "主人偏好一句话说完。  ",
        "target_layer": "L3",
        "target": "people/owner",
        "source": "gen1:fixture/memory-raw.md",
        "topic": "沟通偏好",
        "importance": 4,
    },
    {
        "content": "api_key=do-not-stage",
        "target_layer": "L3",
        "target": "people/owner",
        "source": "gen1:fixture/private.md",
        "importance": 5,
    },
    {
        "content": "x" * 801,
        "target_layer": "L3",
        "target": "people/owner",
        "source": "gen1:fixture/long.md",
        "importance": 4,
    },
    {
        "content": "晚间适合安静交流。",
        "target_layer": "L3",
        "target": "people/owner",
        "source": "gen1:fixture/profile.md",
        "importance": 4,
    },
]}, ensure_ascii=False), encoding="utf-8")

batch = prepare_candidates(load_candidate_input(input_path))
check(len(batch.candidates) == 2 and len(batch.duplicates) == 1 and len(batch.rejected) == 2,
      "dedupe and sensitive/length screening are deterministic")
first = batch.candidates[0]
check(first.status == "pending" and first.privacy == "owner-approved-open" and first.visibility == "family-default-on",
      "all normal candidates retain pending privacy and visibility metadata")
check(any(row.reason == "sensitive_screened" and "credential" in row.sensitive_flags for row in batch.rejected),
      "credential-like text is screened without retaining its content")
cli_out = StringIO()
with redirect_stdout(cli_out):
    cli_code = main(["gen1-stage", "--input", str(input_path), "--dry-run"])
check(cli_code == 0 and "pending=2 duplicates=1 rejected=2" in cli_out.getvalue(), "dry-run CLI only reports candidate counts")

stage_root = tmp / "data" / "sleep" / "staging" / "gen1-import"
try:
    stage_candidates(batch, tmp / "outside", run_id="fixture")
except CandidateInputError:
    check(True, "untrusted staging root is refused")
else:
    check(False, "untrusted staging root is refused")

result = stage_candidates(batch, stage_root, run_id="fixture", allowed_roots=(tmp / "data", tmp / "tmp"))
written = json.loads((result.directory / "candidates.json").read_text(encoding="utf-8"))
report = json.loads((result.directory / "dry-run.json").read_text(encoding="utf-8"))
check(result.pending == 2 and len(written) == 2 and all(item["status"] == "pending" for item in written),
      "staging writes pending candidates only")
check("do-not-stage" not in (result.directory / "candidates.json").read_text(encoding="utf-8")
      and report["rejected"][0]["content_sha256"], "screened content never reaches staging")
check((workspace / "MEMORY.md").read_text(encoding="utf-8") == "真实工作区哨兵\n", "preparation never writes workspace")
try:
    load_candidate_input(tmp / "missing.json")
except CandidateInputError:
    check(True, "missing input fails closed")
else:
    check(False, "missing input fails closed")

print("ALL PASS test_p4_gen1_import" if failed == 0 else f"{failed} FAILED test_p4_gen1_import")
sys.exit(1 if failed else 0)
