from datetime import datetime, timezone

from guanjia.input.contracts import ProcessorResult, ProcessorSpec
from guanjia.input.confidence import CalibrationRegistry
from guanjia.input.processing import ProcessingPipeline, SynchronousTestRunner


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
pipeline = ProcessingPipeline(runner=SynchronousTestRunner())
fallback = ProcessorSpec('fallback', 'v1', ('audio',), ('text',), 1, True)
primary = ProcessorSpec('primary', 'v1', ('audio',), ('text',), 1, True, ('fallback',))
optional = ProcessorSpec('diarize', 'v1', ('audio',), (), 1, False)


def failed(_):
    raise RuntimeError('not available')


def succeeds(_):
    return ProcessorResult('fallback', 'v1', {'text': 'hello'}, 0.8, now)


pipeline.register(fallback, succeeds)
pipeline.register(primary, failed)
pipeline.register(optional, failed)
outcome = pipeline.process('input-1', 'audio', {})
check(outcome.required_failure is None, 'required processor uses fallback')
check(outcome.results[0].fields['text'] == 'hello', 'fallback result returned')
check(
    outcome.enhancement_failures['diarize'] == 'diarize: processor_error:RuntimeError',
    'enhancement failure does not block',
)
conflicting = ProcessingPipeline(runner=SynchronousTestRunner())
conflicting.register(
    ProcessorSpec('first', 'v1', ('text',), ('value',), 1, True),
    lambda _: ProcessorResult('first', 'v1', {'value': 'one'}, 0.9, now),
)
conflicting.register(
    ProcessorSpec('second', 'v1', ('text',), ('value',), 1, True),
    lambda _: ProcessorResult('second', 'v1', {'value': 'two'}, 0.9, now),
)
check(
    conflicting.process('input-2', 'text', {}).required_failure == 'processor output field conflict: value',
    'processor output collisions are explicit',
)
calibrators = CalibrationRegistry()
calibrators.register('primary-calibrator', lambda _: 0.1)
calibrators.register('fallback-calibrator', lambda _: 0.7)
calibrated = ProcessingPipeline(calibrators, runner=SynchronousTestRunner())
calibrated.register(
    ProcessorSpec(
        'fallback-calibrated', 'v1', ('audio',), ('text',), 1, True,
        calibrator_name='fallback-calibrator',
    ),
    lambda _: ProcessorResult('fallback-calibrated', 'v1', {'text': 'fallback'}, 0.3, now),
)
calibrated.register(
    ProcessorSpec(
        'primary-calibrated', 'v1', ('audio',), ('text',), 1, True,
        ('fallback-calibrated',), calibrator_name='primary-calibrator',
    ),
    failed,
)
check(
    calibrated.process('input-3', 'audio', {}).current_result.confidence == 0.7,
    'fallback uses its own calibrator',
)
returned_error = ProcessingPipeline(runner=SynchronousTestRunner())
returned_error.register(
    ProcessorSpec('error-result', 'v1', ('text',), ('value',), 1, False),
    lambda _: ProcessorResult('error-result', 'v1', {'value': 'ignored'}, 0.9, now, 'model_failed'),
)
check(
    returned_error.process('input-4', 'text', {}).enhancement_failures['error-result']
    == 'error-result: processor_result:model_failed',
    'ProcessorResult.error is treated as a processor failure',
)
print('ALL PASS test_input_processing')
