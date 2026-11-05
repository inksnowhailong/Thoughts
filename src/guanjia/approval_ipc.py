"""语音侧审批 IPC：只收摘要和 decision，永不读取 pending 的原始工具参数。

Phase 5 全量切换：缺省指向 bridge 审批口 http://127.0.0.1:18801，
鉴权用 Bearer（agent-runtime/config/runtime.json 的 token，与 bridge 同值）。
GUANJIA_APPROVAL_URL / GUANJIA_APPROVAL_CAP 可覆盖（OpenClaw 旧守卫回滚用：
设 URL+CAP 后走旧 loopback 语义，不设且 runtime.json 缺失时才读 approval-ipc.json）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

IPC_FILE = Path.home() / ".openclaw" / "workspace" / "guanjia" / "approval-ipc.json"
DEFAULT_BRIDGE = "http://127.0.0.1:18801"
_RUNTIME_TOKEN = Path(__file__).resolve().parents[2] / "agent-runtime" / "config" / "runtime.json"


def _runtime_token() -> str:
    try:
        data = json.loads(_RUNTIME_TOKEN.read_text(encoding="utf-8"))
        token = data.get("token")
        return token.strip() if isinstance(token, str) else ""
    except (OSError, ValueError):
        return ""


class ApprovalIpc:
    """审批口最小客户端（Phase 5 缺省 bridge；env 可回切旧守卫）。"""

    def _config(self) -> dict | None:
        base = os.environ.get("GUANJIA_APPROVAL_URL", "").strip()
        if base:
            cap = os.environ.get("GUANJIA_APPROVAL_CAP", "").strip()
            return {"base": base.rstrip("/"), "capability": cap} if cap else None
        token = _runtime_token()
        if token:
            return {"base": DEFAULT_BRIDGE, "bearer": token}
        try:
            data = json.loads(IPC_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data.get("port"), int) and isinstance(data.get("capability"), str) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _headers(cfg: dict) -> dict[str, str]:
        if cfg.get("bearer"):
            return {"Authorization": f"Bearer {cfg['bearer']}"}
        return {"X-Guanjia-Approval-Cap": str(cfg.get("capability") or "")}

    def latest(self) -> dict | None:
        cfg = self._config()
        if not cfg:
            return None
        url = cfg.get("base") or f"http://127.0.0.1:{cfg['port']}"
        try:
            response = httpx.get(f"{url}/latest",
                                 headers=self._headers(cfg), timeout=0.5, trust_env=False)
            return response.json().get("pending") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def decide(self, pending_id: str, decision: str) -> bool:
        cfg = self._config()
        if not cfg or decision not in ("approve", "reject"):
            return False
        url = cfg.get("base") or f"http://127.0.0.1:{cfg['port']}"
        try:
            response = httpx.post(f"{url}/decide",
                                  headers=self._headers(cfg),
                                  json={"id": pending_id, "decision": decision},
                                  timeout=0.5, trust_env=False)
            return response.status_code == 200 and response.json().get("ok") is True
        except (httpx.HTTPError, ValueError):
            return False
