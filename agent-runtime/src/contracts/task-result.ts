/**
 * TaskResult / Evidence / StructuredError 的 Zod 镜像,
 * 逐字段对齐 `src/guanjia/core/contracts.py`。
 *
 * 注意:
 * - Python `Evidence` 构造时会计算 `content_hash` 并在 `evidence_id` 缺省时以
 *   content_hash 填充;TS 侧仅透传(evidence_id 缺省归一为 null),
 *   Python 端始终是哈希的唯一事实来源。
 * - `TaskResult.artifacts` 在 Python 侧不做元素类型检查,Ts 侧仍按
 *   `ArtifactRef` wire 形状严格校验(wire 严格化,不影响合法负载)。
 */
import { z } from "zod";
import { artifactRefFromWire, artifactRefToDict, artifactRefWireSchema, ArtifactRef } from "./task-spec.js";
import { formatUtc, identifierSchema, isoUtcSchema, jsonValueSchema, JsonValue, parseUtc } from "./ids.js";

/** 镜像 `core/contracts.py::StructuredError`。 */
export const structuredErrorWireSchema = z
  .object({
    code: identifierSchema,
    message: identifierSchema,
    retryable: z.boolean().optional(),
    details: z.record(z.string(), jsonValueSchema).optional(),
  })
  .strict();

export type StructuredErrorWire = z.infer<typeof structuredErrorWireSchema>;

export interface StructuredError {
  code: string;
  message: string;
  retryable: boolean;
  details: { [key: string]: JsonValue };
}

export const structuredErrorSchema = structuredErrorWireSchema.transform((wire) => ({
  code: wire.code,
  message: wire.message,
  retryable: wire.retryable ?? false,
  details: wire.details ?? {},
}));

export function structuredErrorFromWire(wire: StructuredErrorWire): StructuredError {
  return {
    code: wire.code,
    message: wire.message,
    retryable: wire.retryable ?? false,
    details: wire.details ?? {},
  };
}

export function structuredErrorToDict(error: StructuredError): StructuredErrorWire {
  return {
    code: error.code,
    message: error.message,
    retryable: error.retryable,
    details: error.details,
  };
}

/** 镜像 `core/contracts.py::Evidence`。 */
export const evidenceWireSchema = z
  .object({
    kind: identifierSchema,
    summary: identifierSchema,
    observed_at: isoUtcSchema.optional(),
    artifacts: z.array(artifactRefWireSchema).optional(),
    details: z.record(z.string(), jsonValueSchema).optional(),
    evidence_id: identifierSchema.nullable().optional(),
  })
  .strict();

export type EvidenceWire = z.infer<typeof evidenceWireSchema>;

export interface Evidence {
  kind: string;
  summary: string;
  observed_at: Date;
  artifacts: ArtifactRef[];
  details: { [key: string]: JsonValue };
  evidence_id: string | null;
}

export const evidenceSchema = evidenceWireSchema.transform((wire) => ({
  kind: wire.kind,
  summary: wire.summary,
  observed_at: wire.observed_at === undefined ? new Date() : parseUtc(wire.observed_at),
  artifacts: (wire.artifacts ?? []).map(artifactRefFromWire),
  details: wire.details ?? {},
  evidence_id: wire.evidence_id ?? null,
}));

export function evidenceFromWire(wire: EvidenceWire): Evidence {
  return evidenceSchema.parse(wire);
}

export function evidenceToDict(evidence: Evidence): EvidenceWire {
  return {
    kind: evidence.kind,
    summary: evidence.summary,
    observed_at: formatUtc(evidence.observed_at),
    artifacts: evidence.artifacts.map(artifactRefToDict),
    details: evidence.details,
    evidence_id: evidence.evidence_id,
  };
}

/** 镜像 `core/contracts.py::TaskResult`。 */
export const taskResultWireSchema = z
  .object({
    result_id: identifierSchema,
    task_id: identifierSchema,
    revision: z.number().int().min(1),
    summary: identifierSchema,
    artifacts: z.array(artifactRefWireSchema).optional(),
    evidence: z.array(evidenceWireSchema).optional(),
    error: structuredErrorWireSchema.nullable().optional(),
    key_steps: z.array(identifierSchema).optional(),
    evidence_at: isoUtcSchema.optional(),
  })
  .strict();

export type TaskResultWire = z.infer<typeof taskResultWireSchema>;

export const taskResultSchema = taskResultWireSchema.transform((wire) => ({
  result_id: wire.result_id,
  task_id: wire.task_id,
  revision: wire.revision,
  summary: wire.summary,
  artifacts: (wire.artifacts ?? []).map(artifactRefFromWire),
  evidence: (wire.evidence ?? []).map(evidenceFromWire),
  error: wire.error === undefined || wire.error === null ? null : structuredErrorFromWire(wire.error),
  key_steps: wire.key_steps ?? [],
  evidence_at: wire.evidence_at === undefined ? new Date() : parseUtc(wire.evidence_at),
}));

export type TaskResult = z.infer<typeof taskResultSchema>;

export function taskResultFromDict(value: unknown): TaskResult {
  return taskResultSchema.parse(value);
}

/** 全字段显式输出(null 保留),便于双端 round-trip 逐键对比。 */
export function taskResultToDict(result: TaskResult): TaskResultWire {
  return {
    result_id: result.result_id,
    task_id: result.task_id,
    revision: result.revision,
    summary: result.summary,
    artifacts: result.artifacts.map(artifactRefToDict),
    evidence: result.evidence.map(evidenceToDict),
    error: result.error === null ? null : structuredErrorToDict(result.error),
    key_steps: [...result.key_steps],
    evidence_at: formatUtc(result.evidence_at),
  };
}
