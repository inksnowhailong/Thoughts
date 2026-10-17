"""Pluggable input processing with required, enhancement, fallback and timeout policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from guanjia.contracts import JsonValue

from .confidence import CalibrationRegistry
from .contracts import CurrentProcessingResult, ProcessorResult, ProcessorSpec

Processor = Callable[[Mapping[str, JsonValue]], ProcessorResult]


class ProcessorRunner(Protocol):
    """Execution boundary: real timeout enforcement belongs to a later adapter."""

    def run(
        self, handler: Processor, payload: Mapping[str, JsonValue], timeout_seconds: float
    ) -> ProcessorResult:
        """Run one processor or raise its execution failure."""


class SynchronousTestRunner:
    """Test-only runner; production must inject a timeout-capable runner."""

    def run(
        self, handler: Processor, payload: Mapping[str, JsonValue], timeout_seconds: float
    ) -> ProcessorResult:
        return handler(payload)


@dataclass(frozen=True)
class ProcessingOutcome:
    """One merged current result plus non-blocking enhancement failures."""

    current_result: CurrentProcessingResult | None
    enhancement_failures: Mapping[str, str]
    required_failure: str | None = None

    @property
    def results(self) -> tuple[CurrentProcessingResult, ...]:
        """Compatibility view: processing now persists one merged current result."""
        return () if self.current_result is None else (self.current_result,)


class ProcessingPipeline:
    """Runs declared processors without depending on ASR, voice, or external agents."""

    def __init__(
        self,
        calibrators: CalibrationRegistry | None = None,
        runner: ProcessorRunner | None = None,
        confidence_aggregator: Callable[[tuple[float, ...]], float] | None = None,
    ) -> None:
        self.calibrators = calibrators or CalibrationRegistry()
        if runner is None:
            raise ValueError('runner is required; inject a timeout-capable execution adapter')
        self.runner = runner
        self.confidence_aggregator = confidence_aggregator or min
        self._specs: dict[str, ProcessorSpec] = {}
        self._handlers: dict[str, Processor] = {}

    def register(self, spec: ProcessorSpec, handler: Processor) -> None:
        if spec.name in self._specs:
            raise ValueError(f'processor already registered: {spec.name}')
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def process(
        self,
        input_id: str,
        input_type: str,
        payload: Mapping[str, JsonValue],
        *,
        now: datetime | None = None,
    ) -> ProcessingOutcome:
        results: list[CurrentProcessingResult] = []
        enhancement_failures: dict[str, str] = {}
        fallback_only = {
            fallback_name
            for spec in self._specs.values()
            for fallback_name in spec.fallback_names
        }
        for spec in self._specs.values():
            if spec.name in fallback_only or input_type not in spec.input_types:
                continue
            executed_spec, result, error = self._run_with_fallback(spec, input_type, payload)
            if error is not None:
                if spec.required:
                    return ProcessingOutcome(None, MappingProxyType(enhancement_failures), f'{spec.name}: {error}')
                enhancement_failures[spec.name] = error
                continue
            assert result is not None
            assert executed_spec is not None
            confidence = self.calibrators.calibrate(
                executed_spec.calibrator_name, result.raw_confidence
            )
            results.append(
                CurrentProcessingResult(
                    input_id=input_id,  # EventId is nominal at type-check time only.
                    processor_name=result.processor_name,
                    processor_version=result.processor_version,
                    fields=result.fields,
                    confidence=confidence,
                    processed_at=result.completed_at if result.completed_at else now or datetime.now(timezone.utc),
                )
            )
        try:
            merged = self._merge(input_id, results, now)
        except ValueError as exc:
            return ProcessingOutcome(None, MappingProxyType(enhancement_failures), str(exc))
        return ProcessingOutcome(merged, MappingProxyType(enhancement_failures))

    def _run_with_fallback(
        self, spec: ProcessorSpec, input_type: str, payload: Mapping[str, JsonValue]
    ) -> tuple[ProcessorSpec | None, ProcessorResult | None, str | None]:
        names = (spec.name, *spec.fallback_names)
        errors: list[str] = []
        for name in names:
            fallback_spec = self._specs.get(name)
            handler = self._handlers.get(name)
            if fallback_spec is None or handler is None:
                errors.append(f'{name}: unavailable')
                continue
            if input_type not in fallback_spec.input_types:
                errors.append(f'{name}: unsupported input type')
                continue
            result, error = self._run(handler, fallback_spec.timeout_seconds, payload)
            if error is None:
                if result.error is not None:
                    errors.append(f'{name}: processor_result:{result.error}')
                    continue
                if result.processor_name != fallback_spec.name or result.processor_version != fallback_spec.version:
                    errors.append(f'{name}: result identity does not match processor spec')
                    continue
                if set(result.fields) != set(fallback_spec.output_fields):
                    errors.append(f'{name}: result fields must exactly match output_fields')
                    continue
                return fallback_spec, result, None
            errors.append(f'{name}: {error}')
        return None, None, '; '.join(errors)

    def _run(
        self, handler: Processor, timeout_seconds: float, payload: Mapping[str, JsonValue]
    ) -> tuple[ProcessorResult | None, str | None]:
        try:
            return self.runner.run(handler, payload, timeout_seconds), None
        except Exception as exc:
            return None, f'processor_error:{type(exc).__name__}'

    def _merge(
        self,
        input_id: str,
        results: list[CurrentProcessingResult],
        now: datetime | None,
    ) -> CurrentProcessingResult | None:
        if not results:
            return None
        fields: dict[str, JsonValue] = {}
        for result in results:
            for key, value in result.fields.items():
                if key in fields:
                    raise ValueError(f'processor output field conflict: {key}')
                fields[key] = value
        confidence = self.confidence_aggregator(tuple(result.confidence for result in results))
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            raise ValueError('confidence_aggregator must return a score between 0 and 1')
        return CurrentProcessingResult(
            input_id=input_id,
            processor_name='input.pipeline',
            processor_version=';'.join(
                f'{result.processor_name}@{result.processor_version}' for result in results
            ),
            fields=fields,
            confidence=float(confidence),
            processed_at=now or datetime.now(timezone.utc),
        )
