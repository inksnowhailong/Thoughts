"""Guanjia 的纯领域核心。"""

from .agent_runtime import MainAgentRuntime, StagedEffect, TurnOutcome
from .contracts import (
    ArtifactRef,
    DeliveryRequirements,
    Evidence,
    PermissionScope,
    ResourceRequest,
    StructuredError,
    TaskResult,
    TaskSpec,
    WorkerEvent,
)
from .main_event_parser import EVENT_SCHEMA_VERSION, EventValidationError, MainEvent, parse_jsonl
from .main_program import CancellationToken, CompleteTurnGenerator, MainProgram, ShadowCommitForbiddenError, ShadowTurnCancelledError, ShadowTurnRequest
from .shadow import ALLOWED_CORE_DIFFERENCE_FIELDS, CoreShadowCoordinator, CoreShadowMetrics
from .model_policy import ModelPolicy, ModelProfile
from .openclaw_adapter import OpenClawAdapter
from .worker_runtime import WorkerRuntime, WorkerSnapshot

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
    'ShadowCommitForbiddenError',
    'ShadowTurnCancelledError',
    'ShadowTurnRequest',
    'MainEvent',
    'ModelPolicy',
    'ModelProfile',
    'OpenClawAdapter',
    'PermissionScope',
    'ResourceRequest',
    'StagedEffect',
    'StructuredError',
    'TaskResult',
    'TaskSpec',
    'TurnOutcome',
    'WorkerEvent',
    'WorkerRuntime',
    'WorkerSnapshot',
    'parse_jsonl',
]
