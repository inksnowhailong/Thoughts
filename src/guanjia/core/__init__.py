"""Guanjia 的纯领域核心。"""

from .agent_runtime import MainAgentRuntime, StagedEffect, TurnOutcome
from .contracts import (
    ArtifactRef,
    DeliveryRequirements,
    Evidence,
    Materials,
    MemoryPacket,
    PermissionScope,
    ResourceRequest,
    StructuredError,
    TaskResult,
    TaskSpec,
    WorkerAdapter,
    WorkerEvent,
)
from .main_event_parser import EVENT_SCHEMA_VERSION, EventValidationError, MainEvent, parse_jsonl
from .main_program import CancellationToken, CompleteTurnGenerator, MainProgram, ShadowCommitForbiddenError, ShadowTurnCancelledError, ShadowTurnRequest
from .shadow import ALLOWED_CORE_DIFFERENCE_FIELDS, CoreShadowCoordinator, CoreShadowMetrics
from .model_policy import ModelPolicy, ModelProfile

__all__ = [
    'EVENT_SCHEMA_VERSION',
    'ArtifactRef',
    'DeliveryRequirements',
    'Evidence',
    'EventValidationError',
    'ALLOWED_CORE_DIFFERENCE_FIELDS',
    'CancellationToken',
    'CompleteTurnGenerator',
    'CoreShadowCoordinator',
    'CoreShadowMetrics',
    'MainAgentRuntime',
    'MainProgram',
    'Materials',
    'MemoryPacket',
    'ShadowCommitForbiddenError',
    'ShadowTurnCancelledError',
    'ShadowTurnRequest',
    'MainEvent',
    'ModelPolicy',
    'ModelProfile',
    'PermissionScope',
    'ResourceRequest',
    'StagedEffect',
    'StructuredError',
    'TaskResult',
    'TaskSpec',
    'TurnOutcome',
    'WorkerAdapter',
    'WorkerEvent',
    'parse_jsonl',
]
