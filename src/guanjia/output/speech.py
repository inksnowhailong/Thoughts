"""语音片段契约。"""

from dataclasses import dataclass

from .contracts import _required


@dataclass(frozen=True)
class SpeechSegment:
    segment_id: str
    output_id: str
    text: str
    sequence: int
    generation_id: str
    conversation_id: str
    tone: str = 'neutral'
    rate: float = 1.0
    pause_after_ms: int = 0
    interruptible: bool = True

    def __post_init__(self) -> None:
        for value, name in (
            (self.segment_id, 'segment_id'), (self.output_id, 'output_id'),
            (self.text, 'text'), (self.generation_id, 'generation_id'),
            (self.conversation_id, 'conversation_id'), (self.tone, 'tone'),
        ):
            _required(value, name)
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError('sequence must be a non-negative integer')
        if type(self.rate) not in (int, float) or not 0.5 <= self.rate <= 2.0:
            raise ValueError('rate must be between 0.5 and 2.0')
        if type(self.pause_after_ms) is not int or self.pause_after_ms < 0:
            raise ValueError('pause_after_ms must be a non-negative integer')
