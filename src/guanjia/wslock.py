"""工作区写锁：面板编辑与 P4 休眠提交共用。锁文件锁第 0 字节（msvcrt.locking），进程退出系统自动释放；不可重入。

    with ws_lock(holder="sleep"):          # P4：一次提交多个文件
        fs.write_unlocked("USER.md", ...)
        fs.write_unlocked("MEMORY.md", ...)

锁只约束「我方代码」。模型用自己的 edit 工具写工作区文件时不拿这把锁，那一边靠 wsfiles 的 base_sha 冲突检测兜底。
"""

from __future__ import annotations

import json
import msvcrt
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path.home() / ".openclaw" / "workspace" / "guanjia" / "ws-write.lock"


class LockBusy(Exception):
    """等超时还拿不到锁；str() 是给人看的持有者信息。"""


def _info_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def _holder(path: Path) -> str:
    try:
        d = json.loads(_info_path(path).read_text(encoding="utf-8"))
        return f"{d.get('holder')}（pid {d.get('pid')}，{d.get('since')} 起）"
    except (OSError, ValueError):
        return "未知"


@contextmanager
def ws_lock(timeout: float = 10.0, holder: str = "panel", path: Path = LOCK_PATH) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(_holder(path)) from None
                time.sleep(0.1)
        try:
            try:  # 持有者信息只是给面板提示用，写不进去不影响锁本身
                _info_path(path).write_text(json.dumps({"holder": holder, "pid": os.getpid(),
                                                        "since": time.strftime("%H:%M:%S")}), encoding="utf-8")
            except OSError:
                pass
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)
