"""事件契约使用的名义标识类型。"""

from typing import NewType


EventId = NewType("EventId", str)
CorrelationId = NewType("CorrelationId", str)
CausationId = NewType("CausationId", str)
IdempotencyKey = NewType("IdempotencyKey", str)


def validate_identifier(value: str, field_name: str) -> str:
    """校验事件契约中必填的非空字符串标识。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
