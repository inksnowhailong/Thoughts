/**
 * Worker API 契约 —— TS 主导的新契约(Python 本阶段不需要),
 * 对应重构计划 §3.3 的 Worker 适配器反转接口:
 * - `POST /v1/worker/runs`         请求体 WorkerRunRequest
 * - `GET  /v1/worker/runs/{id}/events` SSE 事件 WorkerRunEvent(判别联合)
 * - `POST /v1/worker/runs/{id}/resume`  请求体 ResumeRequest
 *
 * 字段语义:
 * - WorkerRunRequest:`run_id` 唯一执行标识;`task_spec` 复用 Python 线协议;
 *   `materials` 限定 Worker 只能看见显式引用(镜像 Materials dataclass);
 *   `memory_packet` ≤8 条摘要;`allowed_capabilities` 是本任务允许的能力子集;
 *   `model_hint` 可选的模型建议(可为 null)。
 * - WorkerRunEvent.progress:percent 0..100 或 null(未知),stage 为当前阶段;
 * - WorkerRunEvent.approval_request:挂起等待审批,action_fingerprint 与
 *   AuthorizationRequest 一致(64 位小写 hex);
 * - WorkerRunEvent.tool_trace:一次能力调用的轻量痕迹;
 * - WorkerRunEvent.outcome:终态(completed/failed/cancelled),
 *   result 仅在 completed 时携带完整 TaskResult 线协议。
 * - ResumeRequest:审批恢复(approve 附带一次性凭证 credential,可为 null)。
 */
import { z } from "zod";
import { identifierSchema } from "./ids.js";
import { artifactRefWireSchema } from "./task-spec.js";
import { memoryPacketWireSchema } from "./memory-packet.js";
import { taskResultWireSchema } from "./task-result.js";
import { taskSpecWireSchema } from "./task-spec.js";

/** 镜像 `core/worker_runtime.py::Materials`:Worker 只见显式引用。 */
export const materialsWireSchema = z
  .object({ references: z.array(artifactRefWireSchema).optional() })
  .strict();

export type MaterialsWire = z.infer<typeof materialsWireSchema>;

export const workerRunRequestSchema = z
  .object({
    run_id: identifierSchema,
    task_spec: taskSpecWireSchema,
    materials: materialsWireSchema,
    memory_packet: memoryPacketWireSchema,
    allowed_capabilities: z.array(identifierSchema),
    model_hint: identifierSchema.nullable().optional(),
  })
  .strict();

export type WorkerRunRequest = z.infer<typeof workerRunRequestSchema>;

export const workerRunProgressEventSchema = z
  .object({
    type: z.literal("progress"),
    percent: z.number().int().min(0).max(100).nullable(),
    stage: identifierSchema,
    message: identifierSchema.nullable().optional(),
  })
  .strict();

export const workerRunApprovalRequestEventSchema = z
  .object({
    type: z.literal("approval_request"),
    request_id: identifierSchema,
    capability: identifierSchema,
    rationale: identifierSchema,
    action_fingerprint: z.string().regex(/^[0-9a-f]{64}$/, {
      message: "action_fingerprint must be a lowercase 64-hex string",
    }),
  })
  .strict();

export const workerRunToolTraceEventSchema = z
  .object({
    type: z.literal("tool_trace"),
    capability_id: identifierSchema,
    ok: z.boolean(),
    duration_ms: z.number().int().min(0),
    summary: identifierSchema.nullable().optional(),
  })
  .strict();

export const workerRunOutcomeEventSchema = z
  .object({
    type: z.literal("outcome"),
    status: z.enum(["completed", "failed", "cancelled"]),
    summary: identifierSchema,
    result: taskResultWireSchema.nullable().optional(),
  })
  .strict();

export const workerRunEventSchema = z.discriminatedUnion("type", [
  workerRunProgressEventSchema,
  workerRunApprovalRequestEventSchema,
  workerRunToolTraceEventSchema,
  workerRunOutcomeEventSchema,
]);

export type WorkerRunEvent = z.infer<typeof workerRunEventSchema>;

export const resumeRequestSchema = z
  .object({
    decision: z.enum(["approve", "reject"]),
    credential: z.string().min(1).nullable().optional(),
  })
  .strict();

export type ResumeRequest = z.infer<typeof resumeRequestSchema>;
