"""Q2 播报门：影音静默只压制后台播报，保留托盘和用户发起回复。"""

import threading

from guanjia.announcement import AnnouncementPolicy


def check(condition: bool, name: str) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


quiet = [True]
policy = AnnouncementPolicy(lambda: quiet[0])
for kind in ("task_completed", "task_failed", "pending", "nudge"):
    decision = policy.should_announce(kind, user_initiated=False)
    check(not decision.speak and decision.reason == "program_quiet", f"quiet suppresses {kind}")
check(policy.should_announce("reply", user_initiated=True).speak, "user initiated reply still speaks in quiet")
quiet[0] = False
check(policy.should_announce("nudge", user_initiated=False).speak, "normal mode preserves background behavior")

import guanjia.voice as voice
from guanjia.audittail import PendingConfirm
from guanjia.events import NullHub


class Tray:
    def __init__(self):
        self.messages = []

    def notify(self, message):
        self.messages.append(message)


events = []
original_emit = voice.elog.emit
voice.elog.emit = lambda *args, **kwargs: events.append((args, kwargs))
try:
    daemon = object.__new__(voice.VoiceDaemon)
    daemon.announcements = AnnouncementPolicy(lambda: True)
    daemon._paused = threading.Event()
    daemon._muted = threading.Event()
    daemon.tray = Tray()
    daemon._turn_marks, daemon._turn_uid = None, None
    daemon.pending, daemon.hub = PendingConfirm(), NullHub()
    daemon._refresh_tray = lambda: None
    daemon.status = lambda: {}
    daemon._send_nudge("promise", "u1")
    check(daemon.tray.messages and any(kwargs.get("kind") == "nudge" for _, kwargs in events),
          "quiet nudge becomes tray and structured event")
    daemon._on_gate({"decision": "pending.open", "summary": "删除文件"})
    check(len(daemon.tray.messages) == 2 and any(kwargs.get("kind") == "pending" for _, kwargs in events),
          "quiet pending retains tray and structured event")
finally:
    voice.elog.emit = original_emit

print("ALL PASS test_announcement")
