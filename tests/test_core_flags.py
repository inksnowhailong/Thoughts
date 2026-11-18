"""CoreFlags 环境变量解析的独立单测。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

from guanjia.core_flags import CoreFlags


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


FLAG_ENV_NAMES = ('GUANJIA_CORE_TASK_V1',)
FLAG_ATTR_NAMES = ('core_task_v1',)


defaults = CoreFlags.from_env({})
check(
    all(getattr(defaults, attribute) is False for attribute in FLAG_ATTR_NAMES)
    and defaults.diagnostics == (),
    'all flags default to false without diagnostics',
)

for environment_name, attribute_name in zip(FLAG_ENV_NAMES, FLAG_ATTR_NAMES):
    for spelling in ('1', 'true', 'yes', 'on', ' TrUe '):
        flags = CoreFlags.from_env({environment_name: spelling})
        check(
            getattr(flags, attribute_name) is True,
            f'{environment_name} accepts true spelling {spelling!r}',
        )
    for spelling in ('0', 'false', 'no', 'off', '', '  OFF  ', '   '):
        flags = CoreFlags.from_env({environment_name: spelling})
        check(
            getattr(flags, attribute_name) is False and flags.diagnostics == (),
            f'{environment_name} accepts false spelling {spelling!r}',
        )

mapping = {
    'GUANJIA_CORE_TASK_V1': 'invalid-task',
    'GUANJIA_UNKNOWN_V1': 'true',
}
before = mapping.copy()
invalid = CoreFlags.from_env(mapping)
check(
    all(getattr(invalid, attribute) is False for attribute in FLAG_ATTR_NAMES),
    'invalid values fail closed',
)
check(
    tuple(diagnostic.name for diagnostic in invalid.diagnostics) == FLAG_ENV_NAMES
    and tuple(diagnostic.raw_value for diagnostic in invalid.diagnostics)
    == tuple(mapping[name] for name in FLAG_ENV_NAMES)
    and all(diagnostic.reason == 'invalid_boolean' for diagnostic in invalid.diagnostics),
    'invalid diagnostics are stable and preserve raw values',
)
check(mapping == before, 'provided mapping is not modified')

unknown = CoreFlags.from_env({'GUANJIA_UNKNOWN_V1': 'true'})
check(unknown.core_task_v1 is False and unknown.diagnostics == (),
      'unknown environment names are ignored')

try:
    defaults.core_task_v1 = True
except FrozenInstanceError:
    pass
else:
    raise AssertionError('CoreFlags is frozen')

try:
    invalid.diagnostics[0].name = 'changed'
except FrozenInstanceError:
    pass
else:
    raise AssertionError('FlagDiagnostic is frozen')

print('ALL PASS test_core_flags')
