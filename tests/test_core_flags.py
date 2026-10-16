"""CoreFlags 环境变量解析的独立单测。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

from guanjia.core_flags import CoreFlags


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


FLAG_ENV_NAMES = (
    'GUANJIA_INPUT_SHADOW_V1',
    'GUANJIA_CORE_SHADOW_V1',
    'GUANJIA_CORE_TASK_V1',
    'GUANJIA_OUTPUT_SHADOW_V1',
    'GUANJIA_PERMISSION_SHADOW_V1',
    'GUANJIA_MEMORY_SHADOW_V1',
    'GUANJIA_SELF_LOOP_SHADOW_V1',
)

FLAG_ATTR_NAMES = (
    'input_shadow_v1',
    'core_shadow_v1',
    'core_task_v1',
    'output_shadow_v1',
    'permission_shadow_v1',
    'memory_shadow_v1',
    'self_loop_shadow_v1',
)


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
    'GUANJIA_INPUT_SHADOW_V1': 'invalid-input',
    'GUANJIA_CORE_SHADOW_V1': 'invalid-core',
    'GUANJIA_CORE_TASK_V1': 'invalid-task',
    'GUANJIA_OUTPUT_SHADOW_V1': 'invalid-output',
    'GUANJIA_PERMISSION_SHADOW_V1': 'invalid-permission',
    'GUANJIA_MEMORY_SHADOW_V1': 'invalid-memory',
    'GUANJIA_SELF_LOOP_SHADOW_V1': 'invalid-self-loop',
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

independent = CoreFlags.from_env(
    {
        'GUANJIA_INPUT_SHADOW_V1': 'true',
        'GUANJIA_CORE_SHADOW_V1': 'true',
        'GUANJIA_OUTPUT_SHADOW_V1': 'true',
        'GUANJIA_PERMISSION_SHADOW_V1': 'true',
        'GUANJIA_MEMORY_SHADOW_V1': 'true',
        'GUANJIA_SELF_LOOP_SHADOW_V1': 'true',
    },
)
check(
    independent.core_task_v1 is False
    and all(
        getattr(independent, attribute) is True
        for attribute in FLAG_ATTR_NAMES
        if attribute != 'core_task_v1'
    ),
    'shadow flags do not enable the formal execution flag',
)

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
