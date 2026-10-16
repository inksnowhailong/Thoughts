"""OpenClaw DTO and path adapter; it never reads an OpenClaw workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import MemoryPacket, MemoryQuery

_WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{index}' for index in range(1, 10)), *(f'LPT{index}' for index in range(1, 10))}
_INVALID_SEGMENT_CHARS = set('<>:"/\\|?*')


class OpenClawMemoryBackend(Protocol):
    def search_memory(self, request: dict[str, object]) -> list[dict[str, object]]:
        """Search textual memory through an infrastructure adapter."""

    def get_memory(self, relative_path: str) -> str | None:
        """Read a path supplied by the domain caller through the adapter."""


@dataclass(frozen=True)
class OpenClawMemoryPath:
    relative_path: str

    @classmethod
    def from_logical_key(cls, key: str) -> 'OpenClawMemoryPath':
        if not isinstance(key, str) or not key or key.startswith('/') or key.startswith('\\') or '\\' in key:
            raise ValueError('logical memory key must be a relative safe path')
        parts = key.split('/')
        if any(
            not part or part in {'.', '..'} or len(part) > 120
            or part.endswith(('.', ' ')) or part.upper() in _WINDOWS_RESERVED
            or any(char in _INVALID_SEGMENT_CHARS or ord(char) < 32 for char in part)
            for part in parts
        ):
            raise ValueError('logical memory key contains an unsafe path segment')
        if parts[0] == 'person' and len(parts) == 2:
            return cls(f'memory/people/{parts[1]}.md')
        if parts[0] == 'topic' and len(parts) == 2:
            return cls(f'memory/topics/{parts[1]}.md')
        if len(parts) == 1:
            return cls(f'memory/{parts[0]}.md')
        raise ValueError('logical memory key must use person/<name>, topic/<name>, or a single topic')


def query_to_dto(query: MemoryQuery) -> dict[str, object]:
    """Map the domain query to a JSON-compatible OpenClaw search request."""
    return {'query': query.text, 'people': list(query.people), 'limit': query.limit}


def packet_to_dto(packet: MemoryPacket) -> dict[str, object]:
    """Map records to a portable DTO without accessing any external backend."""
    return {
        'query': query_to_dto(packet.query),
        'records': [
            {
                'id': record.id,
                'key': record.key,
                'content': record.content,
                'confidence': record.confidence,
                'people': list(record.people),
            }
            for record in packet.records
        ],
    }
