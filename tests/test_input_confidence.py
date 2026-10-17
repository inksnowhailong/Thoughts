from guanjia.input.confidence import CalibrationRegistry, replacement_decision


def check(value, message):
    if not value:
        raise AssertionError(message)


registry = CalibrationRegistry()
registry.register('shift', lambda raw: raw * 0.5)
check(registry.calibrate('shift', 0.8) == 0.4, 'registered calibrator used')
check(replacement_decision(0.5, 0.8, margin=0.2) == (True, False), 'higher confidence replaces')
check(
    replacement_decision(0.5, 0.6, margin=0.2) == (False, True),
    'nearby confidence requires review',
)
check(
    replacement_decision(0.8, 0.2, margin=0.2) == (False, False),
    'lower confidence does not replace',
)
try:
    registry.calibrate('missing', 0.5)
except ValueError as error:
    check(str(error) == 'calibrator is not registered: missing', 'missing calibrator is explicit')
else:
    raise AssertionError('missing calibrator must fail')
print('ALL PASS test_input_confidence')
