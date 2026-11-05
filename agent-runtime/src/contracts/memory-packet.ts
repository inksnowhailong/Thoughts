/**
 * MemoryPacket 的 Zod 镜像,对齐 `src/guanjia/core/worker_runtime.py::MemoryPacket`:
 * 最多 8 条摘要,每条 1..500 字符(按 Unicode 码点计,空串拒绝)。
 * 限长规则用 `superRefine` 实现以保证双端报错语义一致。
 */
import { z } from "zod";
import { codePointLength } from "./ids.js";

const summarySchema = z
  .string()
  .refine((value) => {
    const length = codePointLength(value);
    return length > 0 && length <= 500;
  }, { message: "memory summaries must be short strings" });

export const memoryPacketWireSchema = z
  .object({ summaries: z.array(summarySchema) })
  .strict()
  .superRefine((wire, ctx) => {
    if (wire.summaries.length > 8) {
      ctx.addIssue({
        code: "custom",
        path: ["summaries"],
        message: "memory packet must contain at most eight summaries",
      });
    }
  });

export type MemoryPacketWire = z.infer<typeof memoryPacketWireSchema>;

/** 纯字符串列表,wire 与领域对象同构。 */
export type MemoryPacket = MemoryPacketWire;

export const memoryPacketSchema = memoryPacketWireSchema;

export function memoryPacketFromDict(value: unknown): MemoryPacket {
  return memoryPacketSchema.parse(value);
}

export function memoryPacketToDict(packet: MemoryPacket): MemoryPacketWire {
  return { summaries: [...packet.summaries] };
}
