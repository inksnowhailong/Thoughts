"""Windows 上先把 pip 装的 onnxruntime.dll 载入进程，避免 sherpa-onnx 误绑到 System32 里 Windows 自带的旧版 onnxruntime（1.17，API 不够）。

原理：同名 DLL 一旦已在进程内，后续 LoadLibrary("onnxruntime.dll") 直接复用，不再搜 System32。
"""

from __future__ import annotations

import ctypes
import os
import sys


def preload_onnxruntime() -> None:
    if sys.platform != "win32":
        return
    try:
        import onnxruntime  # noqa: F401  只为定位 capi 目录
    except ImportError:
        return
    capi = os.path.join(os.path.dirname(onnxruntime.__file__), "capi")
    os.add_dll_directory(capi)
    for name in ("onnxruntime_providers_shared.dll", "onnxruntime.dll"):
        path = os.path.join(capi, name)
        if os.path.exists(path):
            ctypes.WinDLL(path)
