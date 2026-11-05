/**
 * 统一工具执行结果 —— TS 主导的新契约(Python 本阶段不需要)。
 *
 * 字段语义:
 * - `capability_id`:能力注册表中的能力标识(Tool Registry 唯一来源);
 * - `ok`:执行是否成功;
 * - `output`:任意 JSON 值(可为 null);
 * - `error`:结构化错误 `{code, message, retryable}`,ok=true 时必须为 null,
 *   ok=false 时必须给出,且此时 output 必须为 null(superRefine 保证);
 * - `artifacts`:执行产出物引用(复用 Python ArtifactRef 线协议形状)。
 */
import { z } from "zod";
import { identifierSchema, jsonValueSchema } from "./ids.js";
import { artifactRefFromWire, artifactRefToDict, artifactRefWireSchema } from "./task-spec.js";

export const toolErrorWireSchema = z
  .object({
    code: identifierSchema,
    message: identifierSchema,
    retryable: z.boolean(),
  })
  .strict();

export type ToolErrorWire = z.infer<typeof toolErrorWireSchema>;

export interface ToolError {
  code: string;
  message: string;
  retryable: boolean;
}

export const toolResultWireSchema = z
  .object({
    capability_id: identifierSchema,
    ok: z.boolean(),
    output: jsonValueSchema,
    error: toolErrorWireSchema.nullable(),
    artifacts: z.array(artifactRefWireSchema),
  })
  .strict()
  .superRefine((result, ctx) => {
    if (result.ok && result.error !== null) {
      ctx.addIssue({ code: "custom", path: ["error"], message: "successful tool results must not carry an error" });
    }
    if (!result.ok && result.error === null) {
      ctx.addIssue({ code: "custom", path: ["error"], message: "failed tool results require an error" });
    }
    if (!result.ok && result.output !== null) {
      ctx.addIssue({ code: "custom", path: ["output"], message: "failed tool results must not carry output" });
    }
  });

export type ToolResultWire = z.infer<typeof toolResultWireSchema>;

export const toolResultSchema = toolResultWireSchema.transform((wire) => ({
  capability_id: wire.capability_id,
  ok: wire.ok,
  output: wire.output,
  error: wire.error === null ? null : { code: wire.error.code, message: wire.error.message, retryable: wire.error.retryable },
  artifacts: wire.artifacts.map(artifactRefFromWire),
}));

export type ToolResult = z.infer<typeof toolResultSchema>;

export function toolResultFromDict(value: unknown): ToolResult {
  return toolResultSchema.parse(value);
}

export function toolResultToDict(result: ToolResult): ToolResultWire {
  return {
    capability_id: result.capability_id,
    ok: result.ok,
    output: result.output,
    error: result.error === null ? null : { code: result.error.code, message: result.error.message, retryable: result.error.retryable },
    artifacts: result.artifacts.map(artifactRefToDict),
  };
}
