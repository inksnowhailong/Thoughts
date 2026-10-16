"""P4 调度器：可靠水位、去重、活动重置和失败审计。"""

from types import SimpleNamespace

from guanjia.sleep.rehearsal import IDLE_SECONDS
from guanjia.sleep.scheduler import RehearsalScheduler


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


now, last, reliable, events, calls = [10_000.0], [0.0], [False], [], []
runner = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(
    status="rehearsed", reason="staged", directory=None, session_key="agent:main:sleep-rehearse-fixture"))
scheduler = RehearsalScheduler(runner, watermark=lambda: last[0], watermark_reliable=lambda: reliable[0],
                               emit=lambda *item: events.append(item), enabled=True, interval=1, clock=lambda: now[0])
scheduler.tick()
check(not calls and scheduler.snapshot()["state"] == "blocked", "unreliable main watermark fails closed")
reliable[0] = True
last[0] = now[0] - IDLE_SECONDS + 1
scheduler.tick()
check(not calls and scheduler.snapshot()["state"] == "watching", "recent activity prevents rehearsal")
last[0] = now[0] - IDLE_SECONDS
scheduler.tick()
check(len(calls) == 1 and calls[0]["last_message_at"] == last[0], "idle window runs rehearsal once")
scheduler.tick()
check(len(calls) == 1, "same idle window is deduplicated")
scheduler.note_activity()
last[0] = now[0] - IDLE_SECONDS
scheduler.tick()
check(len(calls) == 2, "new activity resets deduplication window")
check(all(event[0] != "sleep.rehearsal_failed" for event in events), "fixture records no hidden failure")
print("ALL PASS test_sleep_scheduler")
