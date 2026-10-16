"""P4-6 初代画像候选暂存。

输入必须是控制端本地蒸馏后的候选 JSON，不读取或上传初代原文。该模块只会在
项目 data/tmp 下写 staging；候选始终是 pending，不能修改 workspace。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from guanjia.sleep.staging import ROOT

GEN1_STAGING_ROOT = ROOT / "data" / "sleep" / "staging" / "gen1-import"
MAX_CONTENT_CHARS = 800
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_L1_TARGETS = frozenset({"core.user.owner", "core.user.general", "core.facts"})
_L3_TARGET = re.compile(r"^(people/(owner|wife)|topics/[A-Za-z0-9._-]+)$")
_SENSITIVE = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", re.I)),
    ("credential", re.compile(r"\b(?:api[_ -]?key|password|passwd|token|secret)\s*[:=]\s*\S+", re.I)),
    ("bank_card", re.compile(r"\b(?:\d[ -]?){15,18}\d\b")),
    ("identity_number", re.compile(r"\b\d{17}[\dXx]\b")),
)


class CandidateInputError(ValueError):
    """候选文件不符合 P4-6 暂存格式。"""


@dataclass(frozen=True)
class Candidate:
    id: str
    content: str
    target_layer: str
    target: str
    source: str
    privacy: str
    visibility: str
    content_sha256: str
    topic: str
    importance: int
    status: str = "pending"
    sensitive_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RejectedCandidate:
    source: str
    content_sha256: str
    reason: str
    sensitive_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateBatch:
    candidates: tuple[Candidate, ...]
    duplicates: tuple[RejectedCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]


@dataclass(frozen=True)
class ImportStageResult:
    directory: Path | None
    pending: int
    duplicates: int
    rejected: int


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    """候选不可变字段的指纹；P4-6c 用它拒绝来源/内容/目标被替换后的决定。"""
    fields = {key: candidate.get(key) for key in ("id", "content", "target_layer", "target", "source", "content_sha256")}
    return _digest(json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _sensitive(text: str) -> tuple[str, ...]:
    return tuple(label for label, pattern in _SENSITIVE if pattern.search(text))


def _target_ok(layer: str, target: str) -> bool:
    return (layer == "L1" and target in _L1_TARGETS) or (layer == "L3" and bool(_L3_TARGET.fullmatch(target)))


def validate_candidate_content(content: str) -> str:
    """P4-6b 编辑候选时复用同一套长度与密钥筛查。"""
    normalized = _normalize(content)
    if not normalized:
        raise CandidateInputError("候选内容不能为空")
    if len(normalized) > MAX_CONTENT_CHARS:
        raise CandidateInputError(f"候选内容超过 {MAX_CONTENT_CHARS} 字")
    flags = _sensitive(normalized)
    if flags:
        raise CandidateInputError("候选含敏感串：" + "、".join(flags))
    return normalized


def validate_candidate_target(layer: str, target: str) -> tuple[str, str]:
    """P4-6b 移层只能落到计划声明的 L1/L3 目标。"""
    layer, target = layer.strip(), target.strip()
    if not _target_ok(layer, target):
        raise CandidateInputError("不允许的目标层或目标位置")
    return layer, target


def _rejected(raw: dict[str, Any], content: str, reason: str, flags: tuple[str, ...] = ()) -> RejectedCandidate:
    return RejectedCandidate(str(raw.get("source") or "unknown")[:160], _digest(_normalize(content)), reason, flags)


def prepare_candidates(raw_items: Iterable[dict[str, Any]]) -> CandidateBatch:
    """规范化、去重、筛掉密钥类敏感串；不写任何文件。"""
    candidates: list[Candidate] = []
    duplicates: list[RejectedCandidate] = []
    rejected: list[RejectedCandidate] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise CandidateInputError("候选项必须是对象")
        content = str(raw.get("content") or "")
        normalized = _normalize(content)
        source = str(raw.get("source") or "").strip()
        layer = str(raw.get("target_layer") or "").strip()
        target = str(raw.get("target") or "").strip()
        if not normalized or not source:
            rejected.append(_rejected(raw, content, "missing_content_or_source"))
            continue
        if len(normalized) > MAX_CONTENT_CHARS:
            rejected.append(_rejected(raw, normalized, f"too_long:{len(normalized)}"))
            continue
        flags = _sensitive(normalized)
        if flags:
            # 用户允许画像隐私全开，但密钥、卡号、证件号仍不应离开控制端。
            rejected.append(_rejected(raw, normalized, "sensitive_screened", flags))
            continue
        if not _target_ok(layer, target):
            rejected.append(_rejected(raw, normalized, "invalid_layer_or_target"))
            continue
        dedupe_key = _digest(normalized)
        if dedupe_key in seen:
            duplicates.append(_rejected(raw, normalized, "duplicate"))
            continue
        seen.add(dedupe_key)
        importance = raw.get("importance", 4)
        if not isinstance(importance, int) or not 1 <= importance <= 6:
            rejected.append(_rejected(raw, normalized, "invalid_importance"))
            continue
        digest = _digest(normalized)
        candidate_id = "gen1-" + _digest(f"{source}\0{layer}\0{target}\0{normalized}")[:16]
        candidates.append(Candidate(
            id=candidate_id,
            content=normalized,
            target_layer=layer,
            target=target,
            source=source[:160],
            privacy=str(raw.get("privacy") or "owner-approved-open")[:80],
            visibility=str(raw.get("visibility") or "family-default-on")[:80],
            content_sha256=digest,
            topic=str(raw.get("topic") or "其他")[:40],
            importance=importance,
        ))
    return CandidateBatch(tuple(candidates), tuple(duplicates), tuple(rejected))


def load_candidate_input(path: Path) -> list[dict[str, Any]]:
    """读取控制端已蒸馏的 JSON：顶层数组或 {\"candidates\": [...]}。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CandidateInputError(f"候选文件无法读取：{e}") from e
    rows = raw.get("candidates") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise CandidateInputError("候选文件必须是数组或含 candidates 数组的对象")
    return rows


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def stage_candidates(
    batch: CandidateBatch,
    staging_root: Path = GEN1_STAGING_ROOT,
    *,
    run_id: str | None = None,
    allowed_roots: Iterable[Path] = (ROOT / "data", ROOT / "tmp"),
) -> ImportStageResult:
    """只把 pending 候选写进 data/tmp staging，绝不触碰 SOUL/USER/MEMORY/workspace。"""
    if not any(_inside(staging_root, root) for root in allowed_roots):
        raise CandidateInputError("staging-root 必须位于项目 data/ 或 tmp/")
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-gen1"
    if not _RUN_ID.fullmatch(run_id):
        raise CandidateInputError("run-id 只能含字母、数字、点、短横线和下划线")
    directory = staging_root.resolve() / run_id
    if directory.exists():
        raise FileExistsError(f"staging 已存在：{directory}")
    directory.mkdir(parents=True)
    manifest = {
        "kind": "p4-gen1-import-pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "pending_review",
        "workspace_write": False,
        "candidate_count": len(batch.candidates),
    }
    report = {
        "pending": len(batch.candidates),
        "duplicates": [asdict(row) for row in batch.duplicates],
        "rejected": [asdict(row) for row in batch.rejected],
        "next_step": "P4-6b 面板逐条 accept/edit/reject/move；未接受条目不得进入 workspace。",
    }
    (directory / "candidates.json").write_text(
        json.dumps([asdict(row) for row in batch.candidates], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "dry-run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "README.md").write_text(
        "# P4-6 初代画像暂存\n\n"
        "所有候选均为 `pending`，本目录不是工作区，也不会自动导入任何事实。\n\n"
        "P4-6b 面板只可把 `accept/edit/reject/move` 决定写入同目录的 `decisions.json`；"
        "之后由单独的受控提交器将被采纳项转换为 workspace 操作，并在锁、预算、来源校验后提交。\n",
        encoding="utf-8",
    )
    return ImportStageResult(directory, len(batch.candidates), len(batch.duplicates), len(batch.rejected))
