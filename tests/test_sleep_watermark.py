import json
import tempfile
from pathlib import Path

from guanjia.sleep.watermark import read_main_watermark


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "main-watermark.json"
    path.write_text(json.dumps({"version": 1, "session": "agent:main:main", "last_activity_at": 100, "seq": 3, "source": "wechat"}), encoding="utf-8")
    value = read_main_watermark(path, now=101)
    check(value.valid and value.seq == 3 and value.source == "wechat", "valid persisted watermark is readable")
    path.write_text("{broken", encoding="utf-8")
    check(not read_main_watermark(path, now=101).valid, "corrupt watermark fails closed")
    path.write_text(json.dumps({"version": 1, "session": "agent:main:main", "last_activity_at": 200, "seq": 4, "source": "voice"}), encoding="utf-8")
    check(not read_main_watermark(path, now=101).valid, "future timestamp fails closed")
print("ALL PASS test_sleep_watermark")
