/**
 * WorkerEvent 的 Zod 镜像,逐字段对齐 `src/guanjia/core/contracts.py::WorkerEvent`。
 *
 * - `event_type` 枚举必须与 Python 完全一致(10 个,缺一不可);
 * - `percent`:0..100 整数或 null;
 * - `sequence`:非负整数或 null;
 * - `emitted_at` 缺省时取当前 UTC 时间(对齐 dataclass `default_factory=_utc_now`),
 *   wire 中始终输出(`formatUtc`)。
 */
import { z } from "zod";
import { formatUtc, identifierSchema, isoUtcSchema, jsonValueSchema, parseUtc } from "./ids.js";

/** 镜像 `core/contracts.py::WorkerEvent.__post_init__` 的事件类型集合。 */
export const workerEventTypeSchema = z.enum([
  "accepted",
  "progress",
  "needs_information",
  "needs_permission",
  "subtask_requested",
  "artifact",
  "completed",
  "failed",
  "revision_confirmed",
  "health",
]);

export const workerEventWireSchema = z
  .object({
    task_id: identifierSchema,
    revision: z.number().int().min(1),
    event_type: workerEventTypeSchema,
    stage: identifierSchema,
    emitted_at: isoUtcSchema.optional(),
    percent: z.number().int().min(0).max(100).nullable().optional(),
    payload: z.record(z.string(), jsonValueSchema).optional(),
    worker_event_id: identifierSchema.nullable().optional(),
    sequence: z.number().int().min(0).nullable().optional(),
  })
  .strict();

export type WorkerEventWire = z.infer<typeof workerEventWireSchema>;

export const workerEventSchema = workerEventWireSchema.transform((wire) => ({
  task_id: wire.task_id,
  revision: wire.revision,
  event_type: wire.event_type,
  stage: wire.stage,
  emitted_at: wire.emitted_at === undefined ? new Date() : parseUtc(wire.emitted_at),
  percent: wire.percent ?? null,
  payload: wire.payload ?? {},
  worker_event_id: wire.worker_event_id ?? null,
  sequence: wire.sequence ?? null,
}));

export type WorkerEvent = z.infer<typeof workerEventSchema>;

/** 序列化为 wire 形状(时间戳走 `formatUtc`,null 字段显式保留)。 */
export function workerEventToDict(event: WorkerEvent): WorkerEventWire {
  return {
    task_id: event.task_id,
    revision: event.revision,
    event_type: event.event_type,
    stage: event.stage,
    emitted_at: formatUtc(event.emitted_at),
    percent: event.percent,
    payload: event.payload,
    worker_event_id: event.worker_event_id,
    sequence: event.sequence,
  };
}
