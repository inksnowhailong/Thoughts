"""P4-6b gen1 暂存审阅：只读候选、只写同目录 decisions.json。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from guanjia.fsutil import atomic_write_text
from guanjia.sleep.gen1_import import CandidateInputError, candidate_fingerprint, validate_candidate_content, validate_candidate_target
from guanjia.sleep.staging import ROOT

_KIND = "p4-gen1-import-pending"
_DECISIONS_KIND = "p4-gen1-import-decisions"
_ACTIONS = frozenset({"accept", "edit", "reject", "move"})


class StagingReview:
    """审阅 staging，不知道 workspace 的存在，不能提交候选。"""

    def __init__(self, project_root: Path = ROOT) -> None:
        self.project_root = project_root.resolve()
        self.roots = (self.project_root / "data", self.project_root / "tmp")
        self._lock = threading.RLock()

    def list_all(self, query: dict) -> tuple[int, dict]:
        locator = str(query.get("dir") or "").strip()
        if locator:
            stage = self._stage(locator)
            return 200, {"ok": True, "stage": self._detail(self._load_stage(stage))}
        items = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for manifest in root.rglob("manifest.json"):
                try:
                    stage = self._load_stage(manifest.parent)
                except ValueError:
                    continue
                items.append(self._summary(stage))
        items.sort(key=lambda row: (row["created_at"], row["dir"]), reverse=True)
        return 200, {"ok": True, "items": items}

    def decide(self, body: dict) -> tuple[int, dict]:
        action = str(body.get("action") or "").strip()
        if action not in _ACTIONS:
            raise ValueError("不允许的暂存决定")
        stage = self._stage(str(body.get("dir") or ""))
        candidate_id = str(body.get("id") or "").strip()
        loaded = self._load_stage(stage)
        candidates = {str(item.get("id")): item for item in loaded["candidates"]}
        if candidate_id not in candidates:
            raise ValueError("候选不存在")
        decision: dict[str, Any] = {
            "action": action,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "candidate_sha256": candidate_fingerprint(candidates[candidate_id]),
        }
        if action == "edit":
            decision["content"] = validate_candidate_content(str(body.get("text") or ""))
        elif action == "move":
            layer, target = validate_candidate_target(str(body.get("target_layer") or ""), str(body.get("target") or ""))
            decision["target_layer"], decision["target"] = layer, target
        with self._lock:
            # 重新读取、校验，避免在读取和写入间被替换为非 gen1 staging。
            loaded = self._load_stage(stage)
            decisions = self._read_decisions(stage)
            decisions[candidate_id] = decision
            payload = {"kind": _DECISIONS_KIND, "updated_at": decision["at"], "decisions": decisions}
            atomic_write_text(stage / "decisions.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 200, {"ok": True, "decision": decision, "stage": self._detail(self._load_stage(stage))}

    def _stage(self, locator: str) -> Path:
        if not locator or Path(locator).is_absolute() or "\\" in locator:
            raise ValueError("暂存目录必须是项目内的正斜杠相对路径")
        parts = Path(locator).parts
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("暂存目录路径不合法")
        stage = (self.project_root / Path(locator)).resolve()
        return self._load_stage(stage)["path"]

    def _trusted(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self.roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    def _load_stage(self, path: Path) -> dict:
        path = path.resolve()
        if not self._trusted(path) or not path.is_dir():
            raise ValueError("暂存目录不在可信 data/tmp 根下")
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            candidates = json.loads((path / "candidates.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"暂存目录无法读取：{e}") from e
        if not isinstance(manifest, dict) or manifest.get("kind") != _KIND:
            raise ValueError("不是 P4-6 gen1 暂存目录")
        if not isinstance(candidates, list) or any(not isinstance(item, dict) or not item.get("id") for item in candidates):
            raise ValueError("候选文件格式不合法")
        return {"path": path, "manifest": manifest, "candidates": candidates}

    def _read_decisions(self, stage: Path) -> dict[str, dict]:
        path = stage / "decisions.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"决定文件无法读取：{e}") from e
        decisions = raw.get("decisions") if isinstance(raw, dict) and raw.get("kind") == _DECISIONS_KIND else None
        if not isinstance(decisions, dict) or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in decisions.items()):
            raise ValueError("决定文件格式不合法")
        return decisions

    def _summary(self, loaded: dict) -> dict:
        decisions = self._read_decisions(loaded["path"])
        pending = sum(1 for item in loaded["candidates"] if item["id"] not in decisions)
        return {
            "dir": loaded["path"].relative_to(self.project_root).as_posix(),
            "created_at": loaded["manifest"].get("created_at"),
            "candidates": len(loaded["candidates"]),
            "pending": pending,
        }

    def _detail(self, loaded: dict) -> dict:
        decisions = self._read_decisions(loaded["path"])
        rows = [{**item, "decision": decisions.get(item["id"])} for item in loaded["candidates"]]
        return {**self._summary(loaded), "candidates": rows}
