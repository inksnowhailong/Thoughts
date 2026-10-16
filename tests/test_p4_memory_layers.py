"""P4-3 纯本地分层记忆：fixture 解析、预算和目录 dry-run。

运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_p4_memory_layers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guanjia.sleep import caps  # noqa: E402
from guanjia.sleep.index import rebuild_indexes_dry_run, render_indexes  # noqa: E402
from guanjia.sleep.items import parse_header, parse_items, parse_user_items  # noqa: E402
from guanjia.sleep.layers import budget_report, over_limit_report  # noqa: E402

failed = 0


def check(condition: bool, name: str) -> None:
    global failed
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        failed += 1


USER = """# 用户档案
<!-- layer: L1 | source: fixture | visibility: family-default-on -->

## 主人
<!-- observed: 2026-10-14 | status: active | id: u1 | src: panel | imp: 8 -->
- Prefer 简短回答。

## 通用
<!-- skeleton -->
"""

MEMORY = """# 长期记忆
<!-- layers: L1,L2 | source: fixture | visibility: family-default-on -->

## 关键事实
- 家里有一台音箱。 <!-- id: k1 | src: panel | t: 2026-10-14 | imp: 6 | hits: 0 | lh: 2026-10-14 -->

## 悬而未决

## 最近要点

## 记忆目录
旧目录
"""

OWNER = """# 主人
> 摘要：主人档案。
> 关键词：主人、你
<!-- layer: L3 | source: fixture | visibility: family-default-on -->

## 事实与偏好
- Prefer 安静。 <!-- id: p1 | src: panel | imp: 8 -->
"""

WIFE = """# 老婆
> 摘要：家人档案。
> 关键词：老婆、媳妇
<!-- layer: L3 | source: fixture | visibility: family-default-on -->

## 事实与偏好
"""

TOPIC = """# 家里设备
> 摘要：设备档案。
> 关键词：音箱、设备
<!-- layer: L3 | source: fixture | visibility: family-default-on -->

## 设备
- 客厅有音箱。 <!-- id: t1 | src: panel | imp: 5 -->
"""

files = {
    "USER.md": USER,
    "MEMORY.md": MEMORY,
    "memory/people/主人.md": OWNER,
    "memory/people/老婆.md": WIFE,
    "memory/topics/家里设备.md": TOPIC,
}

# ---- 元数据解析：L1、L2/L3 与文件级 source/visibility 都必须保留 ----
user_items = parse_user_items(USER)
check(user_items["主人"][0].meta["id"] == "u1" and user_items["主人"][0].meta["src"] == "panel", "parses USER metadata")
facts = parse_items(MEMORY)
check(facts[0].meta["src"] == "panel" and facts[0].meta["hits"] == "0", "parses L1 item metadata")
header = parse_header(OWNER)
check(header.title == "主人" and header.meta["visibility"] == "family-default-on", "preserves L3 source and visibility")

# ---- 预算：分节与 L3 文件均被报告，超限文本可供未来面板/runner 使用 ----
report = budget_report(files)
check(any(e.rel == "USER.md" and e.section == "主人" and e.limit == caps.USER_OWNER for e in report), "reports USER section cap")
check(any(e.rel == "MEMORY.md" and e.section == "记忆目录" and e.limit == caps.MEM_INDEX for e in report), "reports L2 section cap")
too_long = dict(files, **{"memory/topics/超长.md": "# 超长\n" + "x" * (caps.LONG_FILE + 1)})
check(any("memory/topics/超长.md" in line for line in over_limit_report(too_long)), "reports long-file overflow")

# ---- 目录 dry-run：稳定、覆盖旧目录、不改输入、不落盘 ----
l2_a, full_a = render_indexes(files, daily_notes=("memory/2026-10-14.md",), timeline_lines=2)
l2_b, full_b = render_indexes(files, daily_notes=("memory/2026-10-14.md",), timeline_lines=2)
check(l2_a == l2_b and full_a == full_b, "index rendering is stable")
planned = rebuild_indexes_dry_run(files, daily_notes=("memory/2026-10-14.md",), timeline_lines=2)
check(files["MEMORY.md"] == MEMORY and "旧目录" not in planned["MEMORY.md"], "dry-run leaves input unchanged")
check("人物：主人（1 条） · 老婆（0 条）" in planned["MEMORY.md"], "L2 directory lists people deterministically")
check("主题：家里设备（1 条）" in planned["MEMORY.md"] and "日记：1 天 · 时间线 2 行" in planned["MEMORY.md"], "L2 directory summarizes topics and timeline")
check("## memory/people/主人.md" in planned["memory/index.md"] and "> 关键词：主人、你" in planned["memory/index.md"], "full index retains headers")

print("ALL PASS test_p4_memory_layers" if failed == 0 else f"{failed} FAILED test_p4_memory_layers")
sys.exit(1 if failed else 0)
