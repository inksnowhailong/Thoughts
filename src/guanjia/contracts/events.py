"""跨模块事件的不可变、可序列化契约。"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping, TypeAlias

from .ids import validate_identifier


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)

_SCHEMA_VERSION = 1
_FIELDS = {
    "event_id",
    "event_type",
    "occurred_at",
    "correlation_id",
    "causation_id",
    "idempotency_key",
    "schema_version",
    "payload",
}


def _freeze_json(value: object) -> FrozenJsonValue:
    """递归验证 JSON 值，同时生成不与调用方共享的不可变副本。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload floats must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("payload object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("payload must contain only JSON values")


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    """将内部冻结 JSON 值还原为调用方可修改的副本。"""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_utc(value: datetime) -> datetime:
    """拒绝 naive 和非 UTC 时间，并统一使用标准 UTC tzinfo。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("occurred_at must be an aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("occurred_at must be UTC")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _validate_utc(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("occurred_at must be an ISO-8601 UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO-8601 UTC Z timestamp") from exc
    return _validate_utc(parsed)


@dataclass(frozen=True)
class EventEnvelope:
    """可跨模块传递的版本化领域事件。"""

    event_id: str
    event_type: str
    occurred_at: datetime
    correlation_id: str
    causation_id: str | None
    idempotency_key: str
    schema_version: int
    payload: Mapping[str, FrozenJsonValue]

    def __post_init__(self) -> None:
        validate_identifier(self.event_id, "event_id")
        validate_identifier(self.event_type, "event_type")
        validate_identifier(self.correlation_id, "correlation_id")
        validate_identifier(self.idempotency_key, "idempotency_key")
        if self.causation_id is not None:
            validate_identifier(self.causation_id, "causation_id")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a JSON object")

        object.__setattr__(self, "occurred_at", _validate_utc(self.occurred_at))
        object.__setattr__(self, "payload", _freeze_json(self.payload))

    @classmethod
    def create(
        cls,
        event_type: str,
        correlation_id: str,
        idempotency_key: str,
        payload: Mapping[str, JsonValue],
        causation_id: str | None = None,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> "EventEnvelope":
        """构造使用 UUID、当前 UTC 时间和当前 schema 的事件。"""
        return cls(
            event_id=event_id if event_id is not None else str(uuid.uuid4()),
            event_type=event_type,
            occurred_at=occurred_at if occurred_at is not None else datetime.now(timezone.utc),
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            schema_version=_SCHEMA_VERSION,
            payload=payload,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        """返回与内部对象不共享可变 payload 的线协议字典。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": _format_utc(self.occurred_at),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
            "payload": _thaw_json(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, JsonValue]) -> "EventEnvelope":
        """从严格的 schema v1 字典反序列化事件。"""
        if not isinstance(value, Mapping) or set(value) != _FIELDS:
            raise ValueError("event envelope fields do not match schema v1")
        return cls(
            event_id=value["event_id"],
            event_type=value["event_type"],
            occurred_at=_parse_utc(value["occurred_at"]),
            correlation_id=value["correlation_id"],
            causation_id=value["causation_id"],
            idempotency_key=value["idempotency_key"],
            schema_version=value["schema_version"],
            payload=value["payload"],
        )

    def to_json(self) -> str:
        """生成稳定、紧凑、保留 Unicode 的 JSON。"""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> "EventEnvelope":
        """从 JSON 文本反序列化事件。"""
        if not isinstance(value, str):
            raise ValueError("event JSON must be a string")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid event JSON") from exc
        return cls.from_dict(decoded)
