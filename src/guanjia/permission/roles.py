"""角色枚举与可注入能力矩阵。"""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Role(str, Enum):
    OWNER = 'owner'
    TRUSTED_FAMILY = 'trusted_family'
    VISITOR = 'visitor'
    SELF_LOOP = 'self_loop'


@dataclass(frozen=True)
class RolePolicy:
    """显式声明角色允许的能力及是否允许不可逆动作。"""

    allowed_capabilities: Mapping[Role, frozenset[str]]
    allow_irreversible: Mapping[Role, bool]
    scope_outside_requires_confirmation: bool = True

    def __post_init__(self) -> None:
        if type(self.scope_outside_requires_confirmation) is not bool:
            raise ValueError('scope_outside_requires_confirmation must be bool')
        if not isinstance(self.allowed_capabilities, Mapping) or not isinstance(self.allow_irreversible, Mapping):
            raise ValueError('role policy inputs must be mappings')
        capabilities: dict[Role, frozenset[str]] = {}
        irreversible: dict[Role, bool] = {}
        for role, values in self.allowed_capabilities.items():
            if type(role) is not Role or not isinstance(values, (set, frozenset, tuple)):
                raise ValueError('role capabilities must map Role to a collection')
            frozen = frozenset(values)
            if not all(isinstance(capability, str) and capability.strip() for capability in frozen):
                raise ValueError('capability must be a non-empty string')
            capabilities[role] = frozen
        for role, allowed in self.allow_irreversible.items():
            if type(role) is not Role or type(allowed) is not bool:
                raise ValueError('irreversibility policy must map Role to bool')
            irreversible[role] = allowed
        object.__setattr__(self, 'allowed_capabilities', MappingProxyType(capabilities))
        object.__setattr__(self, 'allow_irreversible', MappingProxyType(irreversible))

    def allows_capability(self, role: Role, capability: str) -> bool:
        return capability in self.allowed_capabilities.get(role, frozenset())

    def allows_irreversible_action(self, role: Role) -> bool:
        return self.allow_irreversible.get(role, False)
