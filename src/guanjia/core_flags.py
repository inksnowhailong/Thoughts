"""思绪核心的环境变量功能开关。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FlagDiagnostic:
    """无法解析的环境变量值。"""

    name: str
    raw_value: str
    reason: str


@dataclass(frozen=True)
class CoreFlags:
    """核心模块独立的、fail-closed 的功能开关。"""

    input_shadow_v1: bool = False
    core_shadow_v1: bool = False
    core_task_v1: bool = False
    output_shadow_v1: bool = False
    permission_shadow_v1: bool = False
    memory_shadow_v1: bool = False
    self_loop_shadow_v1: bool = False
    diagnostics: tuple[FlagDiagnostic, ...] = ()

    @classmethod
    def from_env(cls, mapping: Mapping[str, str] | None = None) -> 'CoreFlags':
        """从环境变量映射读取开关；未知或非法值一律关闭。"""
        source = os.environ if mapping is None else mapping
        field_names = (
            ('GUANJIA_INPUT_SHADOW_V1', 'input_shadow_v1'),
            ('GUANJIA_CORE_SHADOW_V1', 'core_shadow_v1'),
            ('GUANJIA_CORE_TASK_V1', 'core_task_v1'),
            ('GUANJIA_OUTPUT_SHADOW_V1', 'output_shadow_v1'),
            ('GUANJIA_PERMISSION_SHADOW_V1', 'permission_shadow_v1'),
            ('GUANJIA_MEMORY_SHADOW_V1', 'memory_shadow_v1'),
            ('GUANJIA_SELF_LOOP_SHADOW_V1', 'self_loop_shadow_v1'),
        )
        true_values = frozenset(('1', 'true', 'yes', 'on'))
        false_values = frozenset(('0', 'false', 'no', 'off', ''))
        values: dict[str, bool] = {}
        diagnostics: list[FlagDiagnostic] = []

        for environment_name, field_name in field_names:
            raw_value = source.get(environment_name)
            if raw_value is None:
                values[field_name] = False
                continue

            normalized = raw_value.strip().casefold()
            if normalized in true_values:
                values[field_name] = True
            elif normalized in false_values:
                values[field_name] = False
            else:
                values[field_name] = False
                diagnostics.append(
                    FlagDiagnostic(
                        name=environment_name,
                        raw_value=raw_value,
                        reason='invalid_boolean',
                    ),
                )

        return cls(**values, diagnostics=tuple(diagnostics))
