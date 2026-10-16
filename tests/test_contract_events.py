from datetime import datetime, timezone
import json

from guanjia.contracts.events import EventEnvelope


def check(value, label):
    print(("PASS " if value else "FAIL ") + label)
    if not value:
        raise SystemExit(1)


def raises_value_error(callback):
    try:
        callback()
    except ValueError:
        return True
    return False


def base_payload():
    return {
        "text": "你好，世界",
        "count": 1,
        "ratio": 1.5,
        "enabled": True,
        "empty": None,
        "children": [{"name": "nested"}, [False, 2]],
    }


payload = base_payload()
envelope = EventEnvelope.create(
    "thought.created",
    "correlation-1",
    "idempotency-1",
    payload,
)
check(envelope.schema_version == 1, "default schema version")
check(envelope.event_id and envelope.event_type == "thought.created", "default event id and type")
check(envelope.occurred_at.tzinfo == timezone.utc, "default UTC timestamp")
check(envelope.causation_id is None, "default causation id")
check(envelope.to_dict()["payload"] == base_payload(), "recursive JSON payload")
check(
    isinstance(envelope.payload["children"], tuple)
    and isinstance(envelope.payload["children"][0], type(envelope.payload)),
    "payload uses frozen JSON containers",
)
try:
    envelope.payload["children"][0]["name"] = "forbidden"
    nested_object_frozen = False
except TypeError:
    nested_object_frozen = True
check(nested_object_frozen, "nested payload object is read-only")
wire_payload = envelope.to_dict()["payload"]
check(
    isinstance(wire_payload["children"], list)
    and isinstance(wire_payload["children"][0], dict),
    "to_dict returns wire JSON containers",
)

explicit_at = datetime(2026, 10, 16, 10, 30, 15, 123456, tzinfo=timezone.utc)
explicit = EventEnvelope.create(
    "thought.updated",
    "correlation-2",
    "idempotency-2",
    {"message": "雪"},
    causation_id="cause-2",
    event_id="event-2",
    occurred_at=explicit_at,
)
check(
    (
        explicit.event_id == "event-2"
        and explicit.correlation_id == "correlation-2"
        and explicit.causation_id == "cause-2"
        and explicit.idempotency_key == "idempotency-2"
        and explicit.occurred_at == explicit_at
    ),
    "explicit values",
)

serialized = explicit.to_json()
check(
    serialized
    == json.dumps(
        explicit.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ),
    "stable compact Unicode JSON",
)
roundtrip = EventEnvelope.from_json(serialized)
check(roundtrip == explicit and roundtrip.to_json() == serialized, "JSON roundtrip")
check(EventEnvelope.from_dict(explicit.to_dict()) == explicit, "dict roundtrip")

try:
    explicit.event_type = "mutated"
    frozen = False
except Exception:
    frozen = True
check(frozen, "frozen DTO")

payload["children"][0]["name"] = "changed"
payload["new"] = "outside"
isolated = envelope.to_dict()
check(
    isolated["payload"]["children"][0]["name"] == "nested" and "new" not in isolated["payload"],
    "payload isolated from source mutation",
)
isolated["payload"]["children"][0]["name"] = "returned mutation"
check(
    envelope.to_dict()["payload"]["children"][0]["name"] == "nested",
    "to_dict returns mutable copy",
)

for invalid_payload in (
    {"tuple": (1,)},
    {"set": {1}},
    {"bytes": b"x"},
    {"nan": float("nan")},
    {"infinity": float("inf")},
    {1: "not-string-key"},
):
    check(
        raises_value_error(
            lambda invalid_payload=invalid_payload: EventEnvelope.create(
                "thought.created",
                "correlation",
                "idempotency",
                invalid_payload,
            )
        ),
        "invalid payload rejected",
    )

valid = explicit.to_dict()
invalid_timestamps = (
    datetime(2026, 10, 16, 10, 30, 15).isoformat(),
    datetime(2026, 10, 16, 18, 30, 15, tzinfo=timezone.utc).isoformat(),
)
for timestamp in invalid_timestamps:
    invalid = {**valid, "occurred_at": timestamp}
    check(
        raises_value_error(lambda invalid=invalid: EventEnvelope.from_dict(invalid)),
        "naive or non-UTC timestamp rejected",
    )

check(
    raises_value_error(
        lambda: EventEnvelope.from_dict({**valid, "schema_version": 2})
    ),
    "unknown schema rejected",
)
for field in ("event_id", "event_type", "correlation_id", "idempotency_key"):
    check(
        raises_value_error(
            lambda field=field: EventEnvelope.from_dict({**valid, field: ""})
        ),
        "empty required field rejected",
    )
check(
    raises_value_error(lambda: EventEnvelope.from_dict({**valid, "schema_version": True})),
    "boolean schema version rejected",
)
check(
    raises_value_error(lambda: EventEnvelope.from_dict({**valid, "schema_version": 1.0})),
    "float schema version rejected",
)

print("ALL PASS test_contract_events")
