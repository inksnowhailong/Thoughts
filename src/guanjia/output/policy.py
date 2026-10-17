"""Output channel and authorization boundaries."""

from typing import Protocol

from .contracts import Expression, OutputRequest


class ChannelStrategy(Protocol):
    name: str

    def deliver(self, request: OutputRequest, expression: Expression, operation_id: str) -> str:
        """Idempotently deliver ``operation_id`` and return a channel receipt."""


class OutputAuthorizer(Protocol):
    def allows(self, request: OutputRequest) -> bool:
        """Decide authorization before any channel call."""
