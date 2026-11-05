/**
 * EventEnvelope 的 Zod 镜像,逐字段对齐 `src/guanjia/contracts/events.py`。
 *
 * 严格性:Python `from_dict` 要求字段集合精确匹配 `_FIELDS`(多一/少一都报错),
 * 因此这里用 `.strict()`;`schema_version` 必须是字面量 1(注意:JS 无法区分 1 与 1.0,
 * 而 Python 会用 `type(...) is int` 拒绝 1.0 —— 见 Phase 0 报告中的已知限制)。
 */
import { z } from "zod";
import { formatUtc, isoUtcSchema, jsonValueSchema, nonEmptyIdentifierSchema, parseUtc } from "./ids.js";

export const EVENT_SCHEMA_VERSION = 1 as const;

/** 线协议形状(= Python `to_dict` 输出 / `from_dict` 输入)。 */
export const eventEnvelopeWireSchema = z
  .object({
    event_id: nonEmptyIdentifierSchema,
    event_type: nonEmptyIdentifierSchema,
    occurred_at: isoUtcSchema,
    correlation_id: nonEmptyIdentifierSchema,
    causation_id: nonEmptyIdentifierSchema.nullable(),
    idempotency_key: nonEmptyIdentifierSchema,
    schema_version: z.literal(EVENT_SCHEMA_VERSION),
    payload: z.record(z.string(), jsonValueSchema),
  })
  .strict();

/** 领域对象:`occurred_at` 解析为 Date,其余字段与 Python dataclass 同名。 */
export const eventEnvelopeSchema = eventEnvelopeWireSchema.transform((wire) => ({
  event_id: wire.event_id,
  event_type: wire.event_type,
  occurred_at: parseUtc(wire.occurred_at),
  correlation_id: wire.correlation_id,
  causation_id: wire.causation_id,
  idempotency_key: wire.idempotency_key,
  schema_version: wire.schema_version,
  payload: wire.payload,
}));

export type EventEnvelopeWire = z.infer<typeof eventEnvelopeWireSchema>;
export type EventEnvelope = z.infer<typeof eventEnvelopeSchema>;

/** 镜像 `EventEnvelope.from_dict`(未知输入,失败抛 ZodError)。 */
export function eventEnvelopeFromDict(value: unknown): EventEnvelope {
  return eventEnvelopeSchema.parse(value);
}

/** 镜像 `EventEnvelope.to_dict`:键集合与时间戳格式(`_format_utc`)逐字段一致。 */
export function eventEnvelopeToDict(envelope: EventEnvelope): EventEnvelopeWire {
  return {
    event_id: envelope.event_id,
    event_type: envelope.event_type,
    occurred_at: formatUtc(envelope.occurred_at),
    correlation_id: envelope.correlation_id,
    causation_id: envelope.causation_id,
    idempotency_key: envelope.idempotency_key,
    schema_version: envelope.schema_version,
    payload: envelope.payload,
  };
}
