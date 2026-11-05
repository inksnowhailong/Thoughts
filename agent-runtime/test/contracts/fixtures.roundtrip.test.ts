/**
 * Phase 0 任务B:双端共享契约 fixtures 的 TS 侧 round-trip 验证。
 *
 * 与 tests/test_agent_runtime_contracts.py 消费仓库根 contracts/fixtures/
 * 的同一组 JSON 样例,保证 TS(Zod)与 Python(dataclass)解析一致:
 * - 每个 fixture:schema.parse 成功;
 * - parse 后序列化再 parse 稳定;
 * - 负例(invalid/)必须抛 ZodError。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ZodError } from "zod";
import {
  authorizationRequestSchema,
  authorizationRequestToDict,
  eventEnvelopeFromDict,
  eventEnvelopeToDict,
  memoryPacketFromDict,
  memoryPacketSchema,
  memoryPacketToDict,
  permissionDecisionFromDict,
  permissionDecisionSchema,
  permissionDecisionToDict,
  taskResultFromDict,
  taskResultSchema,
  taskResultToDict,
  taskSpecFromDict,
  taskSpecSchema,
  taskSpecToDict,
  toolResultSchema,
  workerEventSchema,
  workerEventToDict,
  workerRunEventSchema,
  workerRunRequestSchema,
  resumeRequestSchema,
} from "../../src/contracts/index.js";

const fixturesDir = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "contracts",
  "fixtures",
);

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function loadFixture(...segments: string[]): any {
  return JSON.parse(readFileSync(resolve(fixturesDir, ...segments), "utf-8"));
}

describe("shared fixture: event envelope", () => {
  const fixture = loadFixture("event-envelope.json");

  it("parses, serializes and round-trips stably", () => {
    const envelope = eventEnvelopeFromDict(fixture);
    expect(envelope.schema_version).toBe(1);
    expect(envelope.event_id).toBe("evt-2041-dispatch");
    // 序列化输出与共享 fixture 逐键一致(镜像 Python to_dict 对比)。
    expect(eventEnvelopeToDict(envelope)).toEqual(fixture);
    // parse → serialize → parse 稳定。
    expect(eventEnvelopeFromDict(eventEnvelopeToDict(envelope))).toEqual(envelope);
  });

  it("rejects strictness violations", () => {
    expect(() => eventEnvelopeFromDict({ ...fixture, extra: true })).toThrow(ZodError);
    expect(() => eventEnvelopeFromDict({ event_id: "e" })).toThrow(ZodError);
    expect(() => eventEnvelopeFromDict({ ...fixture, schema_version: 2 })).toThrow(ZodError);
    expect(() => eventEnvelopeFromDict({ ...fixture, occurred_at: "2026-11-05T16:30:00+08:00" })).toThrow(ZodError);
    expect(() => eventEnvelopeFromDict({ ...fixture, event_id: "  " })).toThrow(ZodError);
  });
});

describe("shared fixture: task spec", () => {
  const fixture = loadFixture("task-spec.json");

  it("parses, serializes and round-trips stably", () => {
    const spec = taskSpecFromDict(fixture);
    expect(spec.task_id).toBe("task-2041");
    expect(spec.revision).toBe(3);
    expect(spec.permission_scope?.grants).toEqual(["photos.read", "render.slides"]);
    expect(taskSpecToDict(spec)).toEqual(fixture);
    expect(taskSpecFromDict(taskSpecToDict(spec))).toEqual(spec);
  });

  it("rejects strictness violations", () => {
    expect(() => taskSpecFromDict({ ...fixture, extra: true })).toThrow(ZodError);
    expect(() => taskSpecFromDict({ ...fixture, revision: 0 })).toThrow(ZodError);
    expect(() => taskSpecFromDict({ ...fixture, resources: [fixture.resources[0], fixture.resources[0]] })).toThrow(ZodError);
    expect(() => taskSpecFromDict({ ...fixture, resources: [{ ...fixture.resources[0], mode: "wrong" }] })).toThrow(ZodError);
    expect(() => taskSpecFromDict({ ...fixture, permission_scope: undefined })).toThrow(ZodError);
  });
});

describe("shared fixture: worker event", () => {
  const fixture = loadFixture("worker-event.json");

  it("parses, serializes and round-trips stably", () => {
    const event = workerEventSchema.parse(fixture);
    expect(event.event_type).toBe("progress");
    expect(event.percent).toBe(45);
    expect(event.sequence).toBe(42);
    expect(workerEventToDict(event)).toEqual(fixture);
    expect(workerEventSchema.parse(workerEventToDict(event))).toEqual(event);
  });

  it("rejects unknown event types and out-of-range percent", () => {
    expect(() => workerEventSchema.parse({ ...fixture, event_type: "bogus" })).toThrow(ZodError);
    expect(() => workerEventSchema.parse({ ...fixture, percent: 101 })).toThrow(ZodError);
    expect(() => workerEventSchema.parse({ ...fixture, percent: -1 })).toThrow(ZodError);
    expect(() => workerEventSchema.parse({ ...fixture, sequence: -1 })).toThrow(ZodError);
  });
});

describe("shared fixture: authorization request", () => {
  const fixture = loadFixture("authorization-request.json");

  it("parses, serializes and round-trips stably", () => {
    const request = authorizationRequestSchema.parse(fixture);
    expect(request.actor.role).toBe("owner");
    expect(request.action_fingerprint).toMatch(/^[0-9a-f]{64}$/);
    expect(authorizationRequestToDict(request)).toEqual(fixture);
    expect(authorizationRequestSchema.parse(authorizationRequestToDict(request))).toEqual(request);
  });

  it("rejects uppercase fingerprints (Python does not lowercase)", () => {
    expect(() =>
      authorizationRequestSchema.parse({ ...fixture, action_fingerprint: fixture.action_fingerprint.toUpperCase() }),
    ).toThrow(ZodError);
  });
});

describe("shared fixture: memory packet", () => {
  const fixture = loadFixture("memory-packet.json");

  it("parses, serializes and round-trips stably", () => {
    const packet = memoryPacketFromDict(fixture);
    expect(packet.summaries).toHaveLength(3);
    expect(memoryPacketToDict(packet)).toEqual(fixture);
    expect(memoryPacketFromDict(memoryPacketToDict(packet))).toEqual(packet);
  });

  it("rejects over-long packets and summaries", () => {
    expect(() => memoryPacketSchema.parse({ summaries: Array.from({ length: 9 }, (_, i) => `备忘 ${i}`) })).toThrow(ZodError);
    expect(() => memoryPacketSchema.parse({ summaries: ["x".repeat(501)] })).toThrow(ZodError);
    expect(() => memoryPacketSchema.parse({ summaries: [""] })).toThrow(ZodError);
  });
});

describe("shared fixture: policy decision", () => {
  const fixture = loadFixture("policy-decision.json");

  it("parses, serializes and round-trips stably", () => {
    const decision = permissionDecisionFromDict(fixture);
    expect(decision.outcome).toBe("confirmation_required");
    expect(decision.todo_id).toBe("todo-2041-9931");
    expect(permissionDecisionToDict(decision)).toEqual(fixture);
    expect(permissionDecisionFromDict(permissionDecisionToDict(decision))).toEqual(decision);
  });

  it("rejects the shared invalid fixture and missing todo_id", () => {
    // 双端共享负例:allow 却带 todo_id。
    const invalid = loadFixture("invalid", "policy-decision-allow-with-todo.json");
    expect(() => permissionDecisionSchema.parse(invalid)).toThrow(ZodError);
    // confirmation_required 必须有 todo_id。
    expect(() =>
      permissionDecisionSchema.parse({ outcome: "confirmation_required", reason: "需要确认" }),
    ).toThrow(ZodError);
  });
});

describe("shared fixture: task result", () => {
  const fixture = loadFixture("task-result.json");

  it("parses, serializes and round-trips stably", () => {
    const result = taskResultFromDict(fixture);
    expect(result.revision).toBe(3);
    expect(result.evidence[0].evidence_id).toBe("ev-2041-1");
    expect(result.artifacts[0].sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(taskResultToDict(result)).toEqual(fixture);
    expect(taskResultFromDict(taskResultToDict(result))).toEqual(result);
  });

  it("rejects strictness violations", () => {
    expect(() => taskResultSchema.parse({ ...fixture, extra: true })).toThrow(ZodError);
    expect(() => taskResultSchema.parse({ ...fixture, revision: 0 })).toThrow(ZodError);
    expect(() => taskResultSchema.parse({ ...fixture, evidence: [{ kind: "k" }] })).toThrow(ZodError);
  });
});

describe("TS-owned contracts (no shared fixture required)", () => {
  const taskSpecFixture = loadFixture("task-spec.json");
  const memoryPacketFixture = loadFixture("memory-packet.json");
  const taskResultFixture = loadFixture("task-result.json");

  it("accepts a worker run request embedding the shared fixtures", () => {
    const request = workerRunRequestSchema.parse({
      run_id: "run-2041-01",
      task_spec: taskSpecFixture,
      materials: { references: taskSpecFixture.references },
      memory_packet: memoryPacketFixture,
      allowed_capabilities: ["photos.read", "render.slides"],
      model_hint: "glm-4.7-flash",
    });
    expect(request.run_id).toBe("run-2041-01");
    expect(request.allowed_capabilities).toHaveLength(2);
  });

  it("validates SSE worker run events by type", () => {
    expect(workerRunEventSchema.parse({ type: "progress", percent: 30, stage: "plan" })).toBeTruthy();
    expect(
      workerRunEventSchema.parse({
        type: "approval_request",
        request_id: "perm-2041-5501",
        capability: "photos.read",
        rationale: "动作超出已授予范围",
        action_fingerprint: "7e".repeat(32),
      }),
    ).toBeTruthy();
    expect(
      workerRunEventSchema.parse({ type: "tool_trace", capability_id: "photos.read", ok: true, duration_ms: 120 }),
    ).toBeTruthy();
    expect(
      workerRunEventSchema.parse({ type: "outcome", status: "completed", summary: "任务完成", result: taskResultFixture }),
    ).toBeTruthy();
    expect(() => workerRunEventSchema.parse({ type: "bogus" })).toThrow(ZodError);
  });

  it("enforces tool result ok/error invariants", () => {
    expect(
      toolResultSchema.parse({
        capability_id: "photos.read",
        ok: true,
        output: { files: 3 },
        error: null,
        artifacts: [],
      }),
    ).toBeTruthy();
    expect(() =>
      toolResultSchema.parse({
        capability_id: "photos.read",
        ok: true,
        output: null,
        error: { code: "boom", message: "不应出现", retryable: false },
        artifacts: [],
      }),
    ).toThrow(ZodError);
    expect(() =>
      toolResultSchema.parse({ capability_id: "photos.read", ok: false, output: null, error: null, artifacts: [] }),
    ).toThrow(ZodError);
  });

  it("validates resume requests", () => {
    expect(resumeRequestSchema.parse({ decision: "approve", credential: "cred-2041" })).toBeTruthy();
    expect(resumeRequestSchema.parse({ decision: "reject" })).toBeTruthy();
    expect(() => resumeRequestSchema.parse({ decision: "maybe" })).toThrow(ZodError);
  });
});
