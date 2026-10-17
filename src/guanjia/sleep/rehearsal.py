"""P4 休眠演练：只读快照、独立会话、只写 staging；绝不提交或换新。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from guanjia.sleep.staging import STAGING_ROOT, WorkspaceSnapshot, snapshot_workspace, stage_index_snapshot

IDLE_SECONDS = 60 * 60


class MaintenancePlannerAdapter(Protocol):
    """Adapts memory planning into rehearsal without granting write access."""

    def __call__(
        self,
        snapshot: WorkspaceSnapshot,
        session_view: dict[str, Any],
        now: datetime,
    ) -> object: ...


@dataclass(frozen=True)
class RehearsalResult:
    status: str
    reason: str
    directory: Path | None = None
    session_key: str = ""
    maintenance_plan: object | None = None


def main_idle(last_message_at: float | None, *, now: float | None = None) -> tuple[bool, str]:
    """空闲至少 60 分钟才演练；没有可靠水位时 fail-closed。"""
    if last_message_at is None:
        return False, "主线新消息水位未接入，拒绝演练"
    age = (now if now is not None else time.time()) - last_message_at
    return (age >= IDLE_SECONDS, "主线已空闲至少 60 分钟" if age >= IDLE_SECONDS else "主线空闲不足 60 分钟")


class SleepRehearsal:
    """受控演练器；依赖注入使会话读取/整理模型可 mock，默认不发网络请求。"""

    def __init__(
        self,
        *,
        workspace_snapshot: Callable[[], WorkspaceSnapshot] = snapshot_workspace,
        session_reader: Callable[[], dict[str, Any]] | None = None,
        consolidator: Callable[[str, WorkspaceSnapshot, dict[str, Any]], dict[str, Any]] | None = None,
        maintenance_planner: MaintenancePlannerAdapter | None = None,
        staging_root: Path = STAGING_ROOT,
        clock: Callable[[], float] = time.time,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.workspace_snapshot = workspace_snapshot
        self.session_reader = session_reader or (lambda: {"connected": False, "limitation": "主线期间无新消息复核接口未接入"})
        self.consolidator = consolidator
        self.maintenance_planner = maintenance_planner
        self.staging_root = staging_root
        self.clock = clock
        self.utc_clock = utc_clock

    def run(self, *, last_message_at: float | None, run_id: str | None = None, dry_run: bool = False) -> RehearsalResult:
        ok, reason = main_idle(last_message_at, now=self.clock())
        if not ok:
            return RehearsalResult("skipped", reason)
        session_key = f"agent:main:sleep-rehearse-{uuid.uuid4().hex}"
        snapshot = self.workspace_snapshot()
        session_view = self.session_reader()
        now = self.utc_clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("utc_clock must return an aware UTC datetime")
        maintenance_plan = (
            self.maintenance_planner(snapshot, session_view, now) if self.maintenance_planner else None
        )
        if dry_run:
            return RehearsalResult(
                "dry_run",
                "已验证只读输入并生成维护计划；未写 staging、未调用整理器",
                session_key=session_key,
                maintenance_plan=maintenance_plan,
            )
        run_id = run_id or now.strftime("%Y%m%d-%H%M%S") + "-rehearse"
        stage = stage_index_snapshot(snapshot, self.staging_root, run_id=run_id, maintenance_plan=maintenance_plan)
        result = (self.consolidator(session_key, snapshot, session_view) if self.consolidator else
                  {"accepted": False, "limitation": "未配置独立整理 runner；仅完成 index staging"})
        log = {
            "kind": "p4-sleep-rehearsal",
            "created_at": now.isoformat(),
            "status_code": "rehearsed",
            "idle_code": "idle_threshold_met",
            "session_audit": _session_audit(session_view),
            "consolidation_audit": _consolidation_audit(result),
            "staging_created": True,
            "commit_allowed": False,
            "sessions_reset_allowed": False,
            "workspace_write_allowed": False,
        }
        (stage.directory / "rehearsal.json").write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return RehearsalResult(
            "rehearsed",
            "已写 staging 演练日志；未提交、未换新",
            stage.directory,
            session_key,
            maintenance_plan,
        )


def _session_audit(session_view: dict[str, Any]) -> dict[str, Any]:
    """Persist only fixed session codes and booleans; never free-form session data."""
    return {
        'connection_code': 'connected' if session_view.get('connected') is True else 'disconnected',
        'recheck_available': session_view.get('recheck_available') is True,
    }


def _consolidation_audit(result: dict[str, Any]) -> dict[str, Any]:
    """Persist only an outcome boolean and a finite code derived from it."""
    accepted = result.get('accepted') is True
    return {'accepted': accepted, 'outcome_code': 'accepted' if accepted else 'not_accepted'}
