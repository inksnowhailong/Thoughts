"""面板用的「已记住的放行」读写（参考实现，契约见 docs/contracts/remembered-overrides.md）。

主人口头说「以后都允许」时，分级插件 guanjia-guard 往线上分级表
C:\\Users\\inksn\\.openclaw\\workspace\\guanjia\\levels.json 的 overrides 里追加一条 source 为 "remembered" 的放行规则。
面板只做两件事：列出这些条目；按 id 撤销其中一条。

    读最新文件 → 只改 overrides（别的键原样保留）→ 写同目录临时文件 → os.replace 原子替换
    只删 source == "remembered" 的条目；主人手写的 overrides、rules、tools 一律不动

插件每次工具调用都重新读这个文件，所以撤销立即生效，不用重启网关。
"""

from __future__ import annotations

import json
from pathlib import Path

from guanjia.fsutil import atomic_write_text

LEVELS_PATH = Path.home() / ".openclaw" / "workspace" / "guanjia" / "levels.json"
REMEMBERED = "remembered"

def load_levels(path: Path = LEVELS_PATH) -> dict:
    """读分级表；文件缺失或损坏时抛 ValueError（不能拿空表覆盖主人的文件）。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text)
    except (OSError, ValueError) as e:
        raise ValueError(f"levels.json 读不出来：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("levels.json 顶层不是对象")
    return data


def _dumps(v) -> str:
    """与 JS 的 JSON.stringify 相同：不转义中文、紧凑分隔符（嵌套的 also 条件也要逐字节一致）。"""
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _one_line(v) -> str:
    if not isinstance(v, dict):
        return _dumps(v)
    return "{ " + ", ".join(f"{_dumps(k)}: {_dumps(x)}" for k, x in v.items()) + " }"


def format_levels(levels: dict) -> str:
    """与插件 remember.ts 的 formatLevelsJson 同一排版：顶层每键一行，tools 每个工具一行，rules / overrides 每条一行。"""
    lines = []
    keys = list(levels)
    for i, k in enumerate(keys):
        v = levels[k]
        if isinstance(v, list):
            body = "[]" if not v else "[\n" + ",\n".join("    " + _one_line(x) for x in v) + "\n  ]"
        elif isinstance(v, dict):
            body = "{}" if not v else "{\n" + ",\n".join(f"    {_dumps(kk)}: {_dumps(vv)}" for kk, vv in v.items()) + "\n  }"
        else:
            body = _dumps(v)
        lines.append(f"  {_dumps(k)}: {body}{',' if i < len(keys) - 1 else ''}")
    return "{\n" + "\n".join(lines) + "\n}\n"


def _atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace（实现搬到了 fsutil，行为不变）。"""
    atomic_write_text(path, text)


def list_remembered(path: Path = LEVELS_PATH) -> list[dict]:
    """已记住的放行，按记住的时间先后。只返回面板要显示的字段。"""
    items = []
    for o in load_levels(path).get("overrides") or []:
        if isinstance(o, dict) and o.get("source") == REMEMBERED and o.get("id"):
            items.append({k: o.get(k) for k in ("id", "summary", "tool", "param", "pattern", "createdAt", "expiresAt")})
    items.sort(key=lambda x: str(x.get("createdAt") or ""))
    return items


def revoke(override_id: str, path: Path = LEVELS_PATH) -> tuple[bool, str]:
    """按 id 撤销一条已记住的放行。返回 (成功?, 原因)：
    removed / not_found / not_remembered（id 对上了但不是口头记住的，不许删）/ unreadable。"""
    try:
        levels = load_levels(path)
    except ValueError:
        return False, "unreadable"
    overrides = levels.get("overrides") or []
    hit = next((o for o in overrides if isinstance(o, dict) and o.get("id") == override_id), None)
    if hit is None:
        return False, "not_found"
    if hit.get("source") != REMEMBERED:
        return False, "not_remembered"
    levels["overrides"] = [o for o in overrides if o is not hit]
    _atomic_write(path, format_levels(levels))
    return True, "removed"


def sync_defaults(defaults: Path, path: Path = LEVELS_PATH) -> int:
    """上线用：线上表换成仓库默认表，但保留线上已有的全部 overrides（已记住的和主人手写的）。
    线上文件不存在时直接用默认表；存在但读不出来时抛 ValueError（让操作的人决定）。返回保留的条数。"""
    new = load_levels(defaults)
    kept = load_levels(path).get("overrides") or [] if path.exists() else []
    new["overrides"] = list(kept)
    _atomic_write(path, format_levels(new))
    return len(kept)


def _main(argv: list[str]) -> int:
    """python -m guanjia.remembered_overrides list [levels.json]
    python -m guanjia.remembered_overrides revoke <id> [levels.json]
    python -m guanjia.remembered_overrides sync-defaults <defaults.json> [levels.json]"""
    if argv[:1] == ["list"]:
        for item in list_remembered(Path(argv[1]) if len(argv) > 1 else LEVELS_PATH):
            print(json.dumps(item, ensure_ascii=False))
        return 0
    if argv[:1] == ["revoke"] and len(argv) >= 2:
        ok, reason = revoke(argv[1], Path(argv[2]) if len(argv) > 2 else LEVELS_PATH)
        print(f"revoke {argv[1]}: {reason}")
        return 0 if ok else 1
    if argv[:1] == ["sync-defaults"] and len(argv) >= 2:
        n = sync_defaults(Path(argv[1]), Path(argv[2]) if len(argv) > 2 else LEVELS_PATH)
        print(f"synced overrides={n}")
        return 0
    print(_main.__doc__)
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
