"""输出中断契约。"""

from dataclasses import dataclass
from enum import Enum

from .contracts import _required
from .speech import SpeechSegment


class InterruptKind(str, Enum):
    USER_INPUT = 'user_input'
    SYSTEM = 'system'


@dataclass(frozen=True)
class Interrupt:
    reason: str
    kind: InterruptKind
    generation_id: str | None = None
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.reason, 'reason')
        if type(self.kind) is not InterruptKind or self.generation_id is None:
            raise ValueError('interrupt must include kind and generation boundary')
        if self.generation_id is not None:
            _required(self.generation_id, 'generation_id')
        if self.conversation_id is not None:
            _required(self.conversation_id, 'conversation_id')

    @classmethod
    def for_generation(cls, generation_id: str, reason: str, kind: InterruptKind = InterruptKind.SYSTEM) -> 'Interrupt':
        return cls(reason, kind, generation_id=generation_id)

    @classmethod
    def for_conversation(cls, conversation_id: str, generation_id: str, reason: str, kind: InterruptKind = InterruptKind.SYSTEM) -> 'Interrupt':
        return cls(reason, kind, generation_id=generation_id, conversation_id=conversation_id)

    def matches(self, segment: SpeechSegment) -> bool:
        if self.kind is InterruptKind.SYSTEM and not segment.interruptible:
            return False
        return segment.generation_id == self.generation_id and (
            self.conversation_id is None or segment.conversation_id == self.conversation_id
        )
