"""P2.2 B2：工作区写锁、冲突检测、备份、原子写、白名单。只用临时目录里的假工作区，不碰线上工作区。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p22_wsfiles.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia import wsfiles  # noqa: E402
from guanjia.wslock import LockBusy, ws_lock  # noqa: E402

failed = 0


def check(cond: bool, name: str) -> None:
    global failed
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed += 1


tmp = Path(tempfile.mkdtemp(prefix="gj-ws-"))
ws, hist = tmp / "workspace", tmp / "history"
(ws / "memory").mkdir(parents=True)
(ws / "guanjia").mkdir()
lock = ws / "guanjia" / "ws-write.lock"
fs = wsfiles.WorkspaceFiles(ws, hist, lock_path=lock)
try:
    check(wsfiles.WorkspaceFiles(ws, hist).lock_path == lock, "default lock path follows the workspace root (never the live one)")
    (ws / "USER.md").write_bytes("# U\r\nline\r\n".encode("utf-8"))
    r = fs.read("USER.md")
    check(r["exists"] and r["text"] == "# U\nline\n", "read normalizes CRLF for the editor")

    # 1. 正常保存：保持 CRLF、无 BOM、有备份、有审计
    r2 = fs.write("USER.md", "# U\nline\nnew\n", r["sha"])
    raw = (ws / "USER.md").read_bytes()
    check(raw == "# U\r\nline\r\nnew\r\n".encode("utf-8"), "write keeps original CRLF style, no BOM")
    check(len(fs.history("USER.md")) == 1, "old version backed up")
    check((hist / "audit.jsonl").read_text(encoding="utf-8").count("USER.md") == 1, "audit line written")
    audit = json.loads((hist / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    check(audit["actor"] == "panel" and audit["old"] == r["sha"] and "line" not in json.dumps(audit), "audit has shas, no content")
    check(r2["sha"] != r["sha"], "new sha returned")
    check(not list(ws.glob("*.tmp")), "no temp file left in workspace")

    # 2. 冲突：模型在你打开之后改过
    (ws / "USER.md").write_text("# U\nmodel wrote\n", encoding="utf-8")
    try:
        fs.write("USER.md", "mine\n", r2["sha"])
        check(False, "stale base_sha -> Conflict")
    except wsfiles.Conflict:
        check(True, "stale base_sha -> Conflict")
    check("model wrote" in (ws / "USER.md").read_text(encoding="utf-8"), "conflict leaves file untouched")
    try:
        fs.write("USER.md", "mine\n", "")
        check(False, "empty base_sha on existing file -> Conflict")
    except wsfiles.Conflict:
        check(True, "empty base_sha on existing file -> Conflict")

    # 3. 新建文件：base_sha 为空串；不存在的文件带了旧 sha 也算冲突
    fs.write("MEMORY.md", "# M\n", "")
    check((ws / "MEMORY.md").exists(), "create MEMORY.md with empty base_sha")
    try:
        fs.write("memory/timeline.md", "x\n", "deadbeef")
        check(False, "non-empty base_sha on missing file -> Conflict")
    except wsfiles.Conflict:
        check(True, "non-empty base_sha on missing file -> Conflict")

    # 4. 白名单与越界
    for bad in ("../x.md", "openclaw.json", "memory/../../x.md", "skills/evil/SKILL.md", "memory/notes.md", "user.md"):
        try:
            fs.read(bad)
            check(False, f"reject path {bad}")
        except ValueError:
            check(True, f"reject path {bad}")
    check(fs.read("memory/2026-10-11.md")["exists"] is False, "daily note path allowed")

    # 5. 超长、混合换行
    try:
        fs.write("MEMORY.md", "字" * 20001, fs.read("MEMORY.md")["sha"])
        check(False, "over 20000 chars rejected")
    except ValueError:
        check(True, "over 20000 chars rejected")
    (ws / "SOUL.md").write_bytes(b"a\r\nb\nc\r\n")
    try:
        fs.write("SOUL.md", "a\nb\nc\nd\n", fs.read("SOUL.md")["sha"])
        check(False, "mixed line endings rejected")
    except ValueError:
        check(True, "mixed line endings rejected")
    check((ws / "SOUL.md").read_bytes() == b"a\r\nb\nc\r\n", "mixed-EOL file untouched")

    # 6. 历史只留 20 份 + 恢复
    for i in range(23):
        fs.write("MEMORY.md", f"v{i}\n", fs.read("MEMORY.md")["sha"])
    h = fs.history("MEMORY.md")
    check(len(h) == 20, "history keeps 20")
    check(h[0]["id"] > h[-1]["id"], "history newest first")
    old = fs.history_text("MEMORY.md", h[-1]["id"])
    check(old.startswith("v"), "history_text returns old content")
    check(len(list((hist / "MEMORY.md").glob("*.txt"))) == 20, "history lives outside the workspace")
    try:
        fs.history_text("MEMORY.md", "../../x")
        check(False, "bad version id rejected")
    except ValueError:
        check(True, "bad version id rejected")

    # 7. 校验回调不通过时不写、不备份
    before = len(fs.history("MEMORY.md"))

    def deny(old_text, new_text):
        raise ValueError("no")

    try:
        fs.write("MEMORY.md", "zzz\n", fs.read("MEMORY.md")["sha"], validate=deny)
        check(False, "validate can veto")
    except ValueError:
        check(len(fs.history("MEMORY.md")) == before and fs.read("MEMORY.md")["text"] != "zzz\n", "validate can veto without side effects")

    # 8. 锁：另一个进程拿着锁时，等不到就 LockBusy；它退出后能拿到
    code = ("import sys,time;sys.path.insert(0,r'%s');from guanjia.wslock import ws_lock\n"
            "from pathlib import Path\nwith ws_lock(holder='sleep', path=Path(r'%s')):\n print('HELD',flush=True);time.sleep(3)"
            % (ROOT / "src", lock))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    check(p.stdout.readline().strip() == "HELD", "child holds lock")
    t0 = time.monotonic()
    try:
        with ws_lock(timeout=0.5, path=lock):
            check(False, "busy lock -> LockBusy")
    except LockBusy as e:
        check(time.monotonic() - t0 < 2 and "sleep" in str(e), "busy lock -> LockBusy naming holder")
    try:
        fs.write("MEMORY.md", "while locked\n", fs.read("MEMORY.md")["sha"], timeout=0.3)
        check(False, "write while locked -> LockBusy")
    except LockBusy:
        check(fs.read("MEMORY.md")["text"] != "while locked\n", "write while locked -> LockBusy, file untouched")
    p.wait()
    with ws_lock(timeout=1, path=lock):
        check(True, "lock acquired after holder exits")
    # 同进程不可重入：持锁时再拿一次要超时
    with ws_lock(timeout=1, path=lock):
        try:
            with ws_lock(timeout=0.3, path=lock):
                check(False, "lock is not reentrant")
        except LockBusy:
            check(True, "lock is not reentrant")
    check(not (lock.parent / "ws-write.lock.tmp").exists(), "lock helper leaves no junk")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS test_p22_wsfiles" if failed == 0 else f"{failed} FAILED test_p22_wsfiles")
sys.exit(1 if failed else 0)
