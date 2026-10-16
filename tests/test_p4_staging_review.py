"""P4-6b 暂存审阅 API：临时项目根，不碰真实 staging 或 workspace。"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.control import make_control_server  # noqa: E402
from guanjia.events import EventHub  # noqa: E402
from guanjia.panel import PanelBackend  # noqa: E402
from guanjia.settings import SettingsStore  # noqa: E402
from guanjia.staging_review import StagingReview  # noqa: E402


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


class FakeDaemon:
    def say(self, text): return True, "queued"
    def status(self): return {"view": "listening"}
    def set_sleep_state(self, state): return True
    def control(self, action): return True, action
    def inject(self, text, dry_run): return {}


with tempfile.TemporaryDirectory() as tmp:
    project = Path(tmp) / "project"
    stage = project / "data" / "sleep" / "staging" / "gen1-import" / "fixture"
    stage.mkdir(parents=True)
    workspace = project / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("workspace sentinel\n", encoding="utf-8")
    (stage / "manifest.json").write_text(json.dumps({
        "kind": "p4-gen1-import-pending", "created_at": "2026-10-14T17:00:00+0800",
    }), encoding="utf-8")
    (stage / "candidates.json").write_text(json.dumps([
        {"id": "gen1-a", "content": "主人偏好简短回答。", "target_layer": "L1", "target": "core.user.owner",
         "source": "gen1:fixture/profile.md", "privacy": "owner-approved-open", "visibility": "family-default-on",
         "content_sha256": "a" * 64, "topic": "沟通偏好", "importance": 6, "status": "pending"},
        {"id": "gen1-b", "content": "主人喜欢在夜晚安静聊天。", "target_layer": "L3", "target": "people/owner",
         "source": "gen1:fixture/memory.md", "privacy": "owner-approved-open", "visibility": "family-default-on",
         "content_sha256": "b" * 64, "topic": "作息", "importance": 4, "status": "pending"},
    ], ensure_ascii=False), encoding="utf-8")

    panel = PanelBackend(EventHub(), SettingsStore(None), status=FakeDaemon().status,
                         control=FakeDaemon().control, inject=FakeDaemon().inject, token="test-token")
    panel.attach_gen1_staging(StagingReview(project))
    server = make_control_server(FakeDaemon(), port=0, panel=panel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def request(method: str, path: str, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": f"127.0.0.1:{port}"}
        if token:
            headers["X-Guanjia-Token"] = "test-token"
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, encoded, headers)
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    try:
        status, body = request("GET", "/api/staging/gen1?token=test-token")
        check(status == 200 and body["items"][0]["pending"] == 2, "trusted gen1 staging is listed")
        status, body = request("GET", "/api/staging/gen1?dir=data/sleep/staging/gen1-import/fixture&token=test-token")
        check(status == 200 and len(body["stage"]["candidates"]) == 2, "staging detail returns pending candidates")
        check(request("GET", "/api/staging/gen1", token=False)[0] == 403, "unauthorized staging read is refused")
        check(request("GET", "/api/staging/gen1?dir=../workspace&token=test-token")[0] == 400, "path traversal is refused")
        check(request("GET", "/api/staging/gen1?dir=workspace&token=test-token")[0] == 400, "non-trusted staging is refused")
        check(request("POST", "/api/staging/gen1", {
            "dir": "data/sleep/staging/gen1-import/fixture", "id": "gen1-a", "action": "destroy",
        })[0] == 400, "illegal decision action is refused")

        status, body = request("POST", "/api/staging/gen1", {
            "dir": "data/sleep/staging/gen1-import/fixture", "id": "gen1-a", "action": "edit", "text": "主人偏好一句说完。",
        })
        decisions = json.loads((stage / "decisions.json").read_text(encoding="utf-8"))
        check(status == 200 and decisions["decisions"]["gen1-a"]["content"] == "主人偏好一句说完。"
              and body["stage"]["pending"] == 1, "edit writes only staging decisions")
        status, body = request("POST", "/api/staging/gen1", {
            "dir": "data/sleep/staging/gen1-import/fixture", "id": "gen1-b", "action": "move",
            "target_layer": "L1", "target": "core.user.owner",
        })
        check(status == 200 and decisions["kind"] == "p4-gen1-import-decisions"
              and json.loads((stage / "decisions.json").read_text(encoding="utf-8"))["decisions"]["gen1-b"]["target"] == "core.user.owner",
              "move records target override in decisions")
        status, _ = request("POST", "/api/staging/gen1", {
            "dir": "data/sleep/staging/gen1-import/fixture", "id": "gen1-a", "action": "accept",
        })
        status_reject, _ = request("POST", "/api/staging/gen1", {
            "dir": "data/sleep/staging/gen1-import/fixture", "id": "gen1-b", "action": "reject",
        })
        current = json.loads((stage / "decisions.json").read_text(encoding="utf-8"))["decisions"]
        check(status == 200 and status_reject == 200 and current["gen1-a"]["action"] == "accept"
              and current["gen1-b"]["action"] == "reject", "accept and reject remain staging-only decisions")
        check((workspace / "MEMORY.md").read_text(encoding="utf-8") == "workspace sentinel\n", "decisions never write workspace")
    finally:
        server.shutdown()
        server.server_close()

source = (ROOT / "src" / "guanjia" / "staging_review.py").read_text(encoding="utf-8")
check("WorkspaceFiles" not in source and "ws_lock" not in source and "reload" not in source,
      "review service has no workspace write, lock or reload path")

print("ALL PASS test_p4_staging_review")
