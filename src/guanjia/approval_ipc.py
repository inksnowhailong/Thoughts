"""语音侧审批 IPC：只收摘要和 decision，永不读取 pending 的原始工具参数。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

IPC_FILE = Path.home() / ".openclaw" / "workspace" / "guanjia" / "approval-ipc.json"


class ApprovalIpc:
    """守卫 loopback 服务的最小客户端。"""

    def _config(self) -> dict | None:
        try:
            data = json.loads(IPC_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data.get("port"), int) and isinstance(data.get("capability"), str) else None
        except (OSError, ValueError):
            return None

    def latest(self) -> dict | None:
        cfg = self._config()
        if not cfg:
            return None
        try:
            response = httpx.get(f"http://127.0.0.1:{cfg['port']}/latest",
                                 headers={"X-Guanjia-Approval-Cap": cfg["capability"]}, timeout=0.5, trust_env=False)
            return response.json().get("pending") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def decide(self, pending_id: str, decision: str) -> bool:
        cfg = self._config()
        if not cfg or decision not in ("approve", "reject"):
            return False
        try:
            response = httpx.post(f"http://127.0.0.1:{cfg['port']}/decide",
                                  headers={"X-Guanjia-Approval-Cap": cfg["capability"]},
                                  json={"id": pending_id, "decision": decision},
                                  timeout=0.5, trust_env=False)
            return response.status_code == 200 and response.json().get("ok") is True
        except (httpx.HTTPError, ValueError):
            return False
