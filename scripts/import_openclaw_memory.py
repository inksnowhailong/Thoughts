"""OpenClaw workspace 存量记忆导入（Phase 4）：staging → review → commit。

把 OpenClaw 工作区（USER.md / MEMORY.md / memory/topics|people|daily / timeline.md）
拆成 MemoryCandidate，走 P4 审阅链落进 Python Memory Service（data/memory/memory.sqlite3）：

    # 1) 产出候选 + 暂存（只写 data/sleep/staging/openclaw-memory/<run>/，不碰库）
    python scripts/import_openclaw_memory.py stage
    # 2) 人工审阅暂存目录里的 candidates.json（模型无直写；本脚本是唯一写入通道）
    # 3) 全部接受并落库（也可手工编辑 decisions.json 后再 commit）
    python scripts/import_openclaw_memory.py commit --run <run_id> --approve-all

幂等：同 key 同内容跳过；同 key 不同内容走 MemoryRepository 冲突链（不覆盖）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.memory.contracts import (  # noqa: E402
    MemoryCandidate,
    MemorySource,
    RetentionLevel,
)
from guanjia.memory.service import MAX_RECORD_CHARS, MemoryService  # noqa: E402

WORKSPACE = ROOT / "openclaw" / "workspace"
STAGING_ROOT = ROOT / "data" / "sleep" / "staging" / "openclaw-memory"

# 各来源的入库层级：key 前缀 / source / retention / importance
SOURCE_PLAN = {
    "user": ("user", RetentionLevel.PERMANENT, 8),        # USER.md 稳定指令
    "memory": ("memory", RetentionLevel.LONG_TERM, 7),    # MEMORY.md 长期事实
    "topic": ("topic", RetentionLevel.LONG_TERM, 6),      # memory/topics/*.md
    "person": ("person", RetentionLevel.LONG_TERM, 7),    # memory/people/*.md
    "daily": ("daily", RetentionLevel.ADAPTIVE, 4),       # memory/YYYY-MM-DD.md
    "timeline": ("timeline", RetentionLevel.ADAPTIVE, 3), # memory/timeline.md 逐行
}
_SLUG_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
MAX_CANDIDATES = 400  # 初代导入上限，防一次性灌爆


def _slug(text: str, limit: int = 40) -> str:
    slug = _SLUG_RE.sub("-", text.casefold()).strip("-")
    return (slug[:limit] or hashlib.sha1(text.encode("utf-8")).hexdigest()[:12])


def _chunks(text: str) -> list[str]:
    """把 markdown 拆成 ≤500 字的候选块：优先按标题/条目切，超长按码点硬切。"""
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("#") and current:  # 标题起新块
            blocks.append("\n".join(current))
            current, size = [], 0
        if len(line) > MAX_RECORD_CHARS:
            if current:
                blocks.append("\n".join(current))
                current, size = [], 0
            blocks.append(line[:MAX_RECORD_CHARS])
            continue
        if size + len(line) > MAX_RECORD_CHARS and current:
            blocks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        blocks.append("\n".join(current))
    return [b.strip() for b in blocks if b.strip()]


def collect_candidates() -> list[dict]:
    """扫描 workspace → 候选清单（dict 形状，供 staging 与 commit 共用）。"""
    candidates: list[dict] = []

    def add(source: str, key: str, content: str, people: tuple[str, ...] = ()) -> None:
        if not content.strip() or len(candidates) >= MAX_CANDIDATES:
            return
        prefix, retention, importance = SOURCE_PLAN[source]
        candidates.append({
            "key": f"{prefix}/{key}",
            "content": content.strip()[:MAX_RECORD_CHARS],
            "memory_type": source,
            "source": "system",
            "confidence": 0.9,
            "importance": importance,
            "retention": retention.value,
            "people": list(people),
        })

    user_md = WORKSPACE / "USER.md"
    if user_md.exists():
        for i, block in enumerate(_chunks(user_md.read_text(encoding="utf-8"))):
            add("user", f"{i:02d}-{_slug(block.splitlines()[0])}", block)

    memory_md = WORKSPACE / "MEMORY.md"
    if memory_md.exists():
        for i, block in enumerate(_chunks(memory_md.read_text(encoding="utf-8"))):
            add("memory", f"{i:02d}-{_slug(block.splitlines()[0])}", block)

    topics = WORKSPACE / "memory" / "topics"
    if topics.is_dir():
        for path in sorted(topics.glob("*.md")):
            for i, block in enumerate(_chunks(path.read_text(encoding="utf-8"))):
                add("topic", f"{path.stem}-{i:02d}", block)

    people = WORKSPACE / "memory" / "people"
    if people.is_dir():
        for path in sorted(people.glob("*.md")):
            person = path.stem
            for i, block in enumerate(_chunks(path.read_text(encoding="utf-8"))):
                add("person", f"{person}-{i:02d}", block, people=(person,))

    memory_dir = WORKSPACE / "memory"
    if memory_dir.is_dir():
        for path in sorted(memory_dir.glob("20??-??-??.md")):
            for i, block in enumerate(_chunks(path.read_text(encoding="utf-8"))):
                add("daily", f"{path.stem}-{i:02d}", block)

    timeline = WORKSPACE / "memory" / "timeline.md"
    if timeline.exists():
        for line in timeline.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                add("timeline", f"{_slug(line)}-{hashlib.sha1(line.encode()).hexdigest()[:6]}", line)
    return candidates


def cmd_stage(source: str | None) -> int:
    global WORKSPACE  # noqa: PLW0603
    if source:
        WORKSPACE = Path(source)
    candidates = collect_candidates()
    run_id = time.strftime("%Y%m%dT%H%M%S")
    stage = STAGING_ROOT / run_id
    stage.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": "openclaw-memory-import-pending",
        "run_id": run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": str(WORKSPACE),
        "count": len(candidates),
        "candidates_sha256": hashlib.sha256(
            json.dumps(candidates, ensure_ascii=False).encode("utf-8")).hexdigest(),
    }
    (stage / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (stage / "candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"staged {len(candidates)} candidates → {stage}")
    print("review candidates.json, then: "
          f"python scripts/import_openclaw_memory.py commit --run {run_id} --approve-all")
    return 0


def _load_stage(run_id: str) -> tuple[Path, list[dict], dict]:
    stage = STAGING_ROOT / run_id
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    candidates = json.loads((stage / "candidates.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(json.dumps(candidates, ensure_ascii=False).encode("utf-8")).hexdigest()
    if digest != manifest.get("candidates_sha256"):
        raise SystemExit(f"候选文件与 manifest 摘要不一致（{stage}），拒绝导入")
    return stage, candidates, manifest


def cmd_commit(run_id: str, approve_all: bool) -> int:
    stage, candidates, _ = _load_stage(run_id)
    decisions_path = stage / "decisions.json"
    if approve_all:
        decisions = {c["key"]: "accept" for c in candidates}
        decisions_path.write_text(
            json.dumps({"kind": "openclaw-memory-import-decisions",
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "decisions": decisions}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    elif not decisions_path.exists():
        raise SystemExit("没有 decisions.json：请先人工审阅，或加 --approve-all 全部接受")
    else:
        decisions = json.loads(decisions_path.read_text(encoding="utf-8")).get("decisions", {})

    service = MemoryService(ROOT / "data" / "memory" / "memory.sqlite3")
    written = skipped = 0
    try:
        for candidate in candidates:
            if decisions.get(candidate["key"]) != "accept":
                skipped += 1
                continue
            record = service.get(candidate["key"])
            if record is not None and record.content.strip() == candidate["content"].strip():
                skipped += 1  # 幂等：同 key 同内容跳过
                continue
            service.write(MemoryCandidate(
                key=candidate["key"],
                content=candidate["content"],
                memory_type=candidate["memory_type"],
                source=MemorySource.SYSTEM,
                confidence=float(candidate["confidence"]),
                importance=int(candidate["importance"]),
                retention=RetentionLevel(candidate["retention"]),
                people=tuple(candidate["people"]),
            ))
            written += 1
    finally:
        service.close()
    print(f"committed {written}, skipped {skipped} (of {len(candidates)}) → data/memory/memory.sqlite3")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw workspace 记忆导入（staging/review/commit）")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="扫描 workspace 产出候选并暂存")
    stage.add_argument("--source", default=None, help="工作区路径（缺省仓库 openclaw/workspace；home 工作区传 %USERPROFILE%\\.openclaw\\workspace）")
    commit = sub.add_parser("commit", help="按审阅决定落库")
    commit.add_argument("--run", required=True, help="stage 输出的 run_id")
    commit.add_argument("--approve-all", action="store_true", help="全部接受（生成 decisions.json）")
    args = parser.parse_args()
    if args.command == "stage":
        return cmd_stage(args.source)
    return cmd_commit(args.run, args.approve_all)


if __name__ == "__main__":
    raise SystemExit(main())
