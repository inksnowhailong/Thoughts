"""小文件工具：原子写文本。"""

from __future__ import annotations

import os
import time
from pathlib import Path

_REPLACE_RETRIES = 5


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace，UTF-8 无 BOM，不做换行转换（调用方自己决定 LF/CRLF）。
    临时文件名带 pid 和时间，不会和别的写入方（插件、面板）撞名。
    Windows 上别的进程恰好在读目标文件时 os.replace 可能 PermissionError，短暂重试。"""
    tmp = path.with_name(f"{path.name}.panel-{os.getpid()}-{time.time_ns()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")  # 写到一半失败（磁盘满等）也要清掉半截 .tmp
        for i in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.05)
    finally:
        if tmp.exists():
            tmp.unlink()
