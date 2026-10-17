from guanjia.output import Interrupt, InterruptKind, SpeechSegment


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


segment = SpeechSegment(
    segment_id='segment-1',
    output_id='out-1',
    text='这是一句完整的话。',
    sequence=0,
    generation_id='generation-1',
    conversation_id='conversation-1',
    tone='warm',
    rate=1.1,
    pause_after_ms=300,
    interruptible=True,
)
check(segment.text == '这是一句完整的话。' and segment.rate == 1.1, 'speech segment preserves expression data')

by_generation = Interrupt.for_generation('generation-1', 'user-input')
check(
    by_generation.matches(segment) and not by_generation.matches(
        SpeechSegment(
            segment_id='segment-2',
            output_id='out-2',
            text='另一句。',
            sequence=0,
            generation_id='generation-2',
            conversation_id='conversation-1',
        )
    ),
    'generation interrupt only matches its generation',
)
by_conversation = Interrupt.for_conversation('conversation-1', 'generation-1', 'new-turn', InterruptKind.USER_INPUT)
check(by_conversation.matches(segment), 'conversation interrupt matches all its generations')
check(by_conversation.matches(segment), 'interruptible segment can be interrupted')
check(
    not by_conversation.matches(
        SpeechSegment(
            segment_id='segment-3',
            output_id='out-3',
            text='不可打断。',
            sequence=0,
            generation_id='generation-3',
            conversation_id='conversation-1',
            interruptible=False,
        ),
    ),
    'uninterruptible segment survives guarded interruption',
)
check(
    Interrupt.for_generation('generation-1', 'user turn', InterruptKind.USER_INPUT).matches(
        SpeechSegment('segment-user', 'out-user', '插话停止。', 0, 'generation-1', 'conversation-1', interruptible=False)
    ),
    'user interruption ignores segment interruptible flag',
)

print('ALL PASS test_output_interrupt')
