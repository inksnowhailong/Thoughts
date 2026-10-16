"""O1-B 词表与 nudge 闩锁最小验证。"""

from types import SimpleNamespace

from guanjia.voice import VoiceDaemon, approval_decision


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


for word in ("确认", "允许", "可以", "行", "好", "好啊", "弄吧", "允许关机", "行你关吧", "好去弄吧"):
    check(approval_decision(word) == "approve", f"approve word {word}")
for word in ("不", "不要", "别弄了", "取消", "算了", "等等", "先别关", "不允许", "不行"):
    check(approval_decision(word) == "reject", f"reject word {word}")
check(all(approval_decision(text) != "approve" for text in ("可以吗", "好了好了", "好家伙", "行不行", "好吧那算了", "不对")), "phrases do not overmatch")

daemon = object.__new__(VoiceDaemon)
daemon.pending = SimpleNamespace(current=lambda: "删除文件")
calls = []
daemon._nudge_check = lambda *args: calls.append(args) or False
result = daemon._should_nudge(SimpleNamespace(reply="我去处理", aborted=False, error=""))
check(not result and calls[0][2] == "pending_approval", "pending approval prevents nudge")
print("ALL PASS test_approval_words")
