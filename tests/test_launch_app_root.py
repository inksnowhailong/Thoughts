"""O2 启动器项目根：部署到 workspace 后仍只从受控根回退。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "openclaw" / "workspace" / "tools" / "launch-app.ps1"


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def invoke(script: Path, name: str, project_root: str) -> dict:
    env = dict(os.environ, GUANJIA_PROJECT_ROOT=project_root)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Name", name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"launcher did not return JSON: {result.stderr[-300:]}")
    return json.loads(lines[-1])


text = SOURCE.read_text(encoding="utf-8")
check("GUANJIA_PROJECT_ROOT" in text and "'F:\\coding\\guanjia'" in text, "controlled roots are explicit")
check("Split-Path (Split-Path (Split-Path $PSScriptRoot" not in text, "workspace path is not used as project root")
check("Test-Path -LiteralPath $python -PathType Leaf" in text and "app_index.py" in text, "fallback validates python and index")

with tempfile.TemporaryDirectory(prefix="gj-launch-root-") as tmp:
    base = Path(tmp)
    # 模拟部署后的 C:\Users\inksn\.openclaw\workspace\tools 形状，脚本不应由此反推项目根。
    deployed = base / "C" / "Users" / "inksn" / ".openclaw" / "workspace" / "tools" / "launch-app.ps1"
    deployed.parent.mkdir(parents=True)
    shutil.copy2(SOURCE, deployed)
    apps = deployed.parent.parent / "apps.json"
    apps.write_text(json.dumps({"apps": {"music": {"names": ["网易云音乐"], "target": ""}}}), encoding="utf-8")

    ok = invoke(deployed, "wangyiyunyinyue", str(ROOT))
    check(ok["error"] == "no_target" and ok["app"] == "music", "deployed script uses configured project root for pinyin fallback")

    no_default = deployed.with_name("launch-app-no-default.ps1")
    no_default.write_text(text.replace("'F:\\coding\\guanjia'", "'Z:\\missing-guanjia'"), encoding="utf-8")
    closed = invoke(no_default, "wangyiyunyinyue", str(base / "missing-root"))
    check(closed["error"] == "not_in_map", "without a valid project root fallback fails closed")

print("ALL PASS test_launch_app_root")
