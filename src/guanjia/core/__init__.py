"""Guanjia 的纯领域核心。"""

from .agent_runtime import CommitPort, MainAgentRuntime, StagedEffect, TurnOutcome
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
from .main_program import CancellationToken, ProductionMainProgram, TurnCancelledError
from .model_policy import ModelPolicy, ModelProfile

__all__ = [
    'EVENT_SCHEMA_VERSION',
    'ArtifactRef',
    'CancellationToken',
    'CommitPort',
    'DeliveryRequirements',
    'Evidence',
    'EventValidationError',
    'MainAgentRuntime',
    'MainEvent',
    'Materials',
    'MemoryPacket',
    'ModelPolicy',
    'ModelProfile',
    'PermissionScope',
    'ProductionMainProgram',
    'ResourceRequest',
    'StagedEffect',
    'StructuredError',
    'TaskResult',
    'TaskSpec',
    'TurnCancelledError',
    'TurnOutcome',
    'WorkerAdapter',
    'WorkerEvent',
    'parse_jsonl',
]
