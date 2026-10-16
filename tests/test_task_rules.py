"""O4 主线规则：长活派任务，短活与普通聊天不被误任务化。"""

from pathlib import Path


rules = (Path(__file__).resolve().parents[1] / "openclaw" / "workspace" / "AGENTS.md").read_text(encoding="utf-8")


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


check("超过 5 秒" in rules and "task_start" in rules, "long multi-step work routes to task_start")
check("短操作、普通聊天、单次查询、开已登记程序仍直接做" in rules, "ordinary conversation is not taskified")
check("不会执行模型、下载、shell" in rules and "不会杀外部进程" in rules, "rules state no execution and no process killing")
print("ALL PASS test_task_rules")
