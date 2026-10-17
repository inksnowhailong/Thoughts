import {
  LATCH_TTL_MS,
  UI_DISPLAY_TTL_MS,
  canonicalJson,
  compareShadow,
  matchesConfirmation,
  opaqueActionFingerprint,
  outputExpectation,
  permissionAuditView,
} from "./core-bridge.ts";

const check = (value: unknown, name: string) => {
  if (!value) throw new Error(`FAIL ${name}`);
  console.log(`PASS ${name}`);
};

const expected = outputExpectation("reply", {
  outputId: "output-1",
  actorId: "owner",
  conversationId: "main",
  text: "任务完成",
  channels: ["voice"],
  taskId: "task-1",
});
const comparison = compareShadow("reply", "output-1", expected, {
  ...expected,
  text: "不同回复 Authorization: Bearer abc.def C:/private/demo.mp4?token=abc",
  summary: { nested: ["password=hunter2", "https://host/path?api_key=sk-abcdefgh"] },
});
check(comparison.differences.some((item) => item.field === "text"), "structured output difference is retained");
check(!/C:\/private|movie\.mp4|hunter2|sk-abcdefgh|abc\.def/.test(JSON.stringify(comparison)), "nested content, secrets, URLs, and media paths are redacted");
check(comparison.eventKey === "shadow:reply:output-1", "stable output key supports sink idempotency");
const missingVsNull = compareShadow("reply", "missing-null", {}, { task_id: null });
check(
  missingVsNull.differences.length === 1
    && missingVsNull.differences[0].expected === undefined
    && missingVsNull.differences[0].observed === null,
  "missing and explicit null remain distinct in shadow differences",
);

const now = Date.UTC(2026, 9, 16, 11, 0, 0);
const fingerprint = "a".repeat(64);
const audit = permissionAuditView({
  todoId: "todo-1",
  actorId: "owner",
  sessionId: "agent:main:main",
  taskId: "task-1",
  actionFingerprint: fingerprint.toUpperCase(),
  outcome: "confirmation_required",
}, now);
check(
  Date.parse(audit.displayExpiresAtUtc) - Date.parse(audit.recordedAtUtc) === UI_DISPLAY_TTL_MS
    && Date.parse(audit.latchExpiresAtUtc) - Date.parse(audit.recordedAtUtc) === LATCH_TTL_MS,
  "three minute display and ten minute latch TTLs stay separate",
);
check(matchesConfirmation(audit, {
  todoId: "todo-1",
  actorId: "owner",
  sessionId: "agent:main:main",
  taskId: "task-1",
  actionFingerprint: fingerprint.toUpperCase(),
}, now), "confirmation binds session actor todo task and action");
check(!matchesConfirmation(audit, { ...audit, taskId: "other-task" }, now), "task mismatch cannot match confirmation");
check(!matchesConfirmation(audit, { ...audit }, now + LATCH_TTL_MS), "expired latch cannot match confirmation");
for (const invalid of [NaN, Infinity, undefined, new Date(), JSON.parse('{"__proto__":"unsafe"}')]) {
  try {
    canonicalJson(invalid);
  } catch {
    continue;
  }
  throw new Error("FAIL canonical JSON rejects unsafe runtime values");
}
try {
  opaqueActionFingerprint("fnv1a:12345678");
  throw new Error("FAIL weak action fingerprint is rejected");
} catch (error) {
  if ((error as Error).message.startsWith("FAIL")) throw error;
}
try {
  permissionAuditView({ ...audit, actionFingerprint: fingerprint }, Number.MAX_SAFE_INTEGER);
  throw new Error("FAIL TimeClip range is enforced");
} catch (error) {
  if ((error as Error).message.startsWith("FAIL")) throw error;
}
try {
  matchesConfirmation({ ...audit, latchExpiresAtUtc: "not-utc" }, { ...audit }, now);
  throw new Error("FAIL malformed latch UTC is rejected");
} catch (error) {
  if ((error as Error).message.startsWith("FAIL")) throw error;
}
console.log("PASS unsafe JSON, weak fingerprints, and invalid timestamps are rejected");
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createGuard } from "./guard.ts";
import { evaluateShadowPermission, toShadowPermissionDecision, type ShadowPermissionObservation } from "./core-bridge.ts";
import type { LevelsFile } from "./levels.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const defaults = JSON.parse(fs.readFileSync(path.join(here, "..", "defaults", "levels.json"), "utf8")) as LevelsFile;

check(
  toShadowPermissionDecision("allow") === "would-allow"
    && toShadowPermissionDecision("deny") === "would-deny"
    && toShadowPermissionDecision("confirmation_required") === "would-confirm",
  "permission outcomes map strictly to would-allow / would-deny / would-confirm",
);

const memFiles: Record<string, unknown> = {};
const auditEntries: any[] = [];
const mockStore: any = {
  readJson: (name: string, fallback: unknown) => memFiles[name] ?? fallback,
  writeJson: (name: string, data: unknown) => { memFiles[name] = data; },
  audit: (entry: unknown) => auditEntries.push(entry),
};

const observedShadow: ShadowPermissionObservation[] = [];
const guardWithShadow = createGuard(mockStore, defaults, undefined, {
  permissionShadowEnabled: true,
  observePermissionShadow: (obs) => {
    observedShadow.push(obs);
  },
});

// 1) L0/L1 allow path
const allowRes = guardWithShadow.beforeToolCall(
  { toolName: "get_weather", params: { city: "Hangzhou" }, toolCallId: "call-1" },
  { sessionKey: "agent:main:main", runId: "run-1", trigger: "user" },
);
check(allowRes === undefined, "L0/L1 hook return value is unchanged");

// 2) Client L3 deny path via tool_call
const denyRes = guardWithShadow.beforeToolCall(
  { toolName: "tool_call", params: { name: "unknown_client_tool", source: "client", arguments: {} }, toolCallId: "call-2" },
  { sessionKey: "agent:main:main", runId: "run-1", trigger: "user" },
);
check(denyRes?.block === true, "client L3 remains blocked by legacy Guard");

// 3) L2 confirmation path + latch preservation
const confirmRes = guardWithShadow.beforeToolCall(
  { toolName: "exec", params: { command: "del C:\\secret\\a.txt", ask: "always" }, toolCallId: "outer-exec-1" },
  { sessionKey: "agent:main:main", runId: "run-1", trigger: "user" },
);
check(confirmRes?.block === true, "L2 remains blocked pending confirmation");
const latchBefore = guardWithShadow.approvals.latest("agent:main:main");
check(Boolean(latchBefore), "legacy approval latch opened normally");

// Approve via owner WeChat turn and verify shadow did not consume latch prematurely
const wechatReply = guardWithShadow.onOwnerWechatTurn("agent:main:main", "允许", {
  channel: "openclaw-weixin",
  senderIsOwner: true,
});
check(Boolean(wechatReply), "owner WeChat confirmation still works unchanged");

const approvedExecRes = guardWithShadow.beforeToolCall(
  { toolName: "exec", params: { command: "del C:\\secret\\a.txt", ask: "always" }, toolCallId: "outer-exec-2" },
  { sessionKey: "agent:main:main", runId: "run-1", trigger: "user" },
);
check(
  JSON.stringify(approvedExecRes) === JSON.stringify({ params: { ask: "off" } }),
  "approved latch is consumed only by real Guard execution and preserves askRewrite",
);

check(
  observedShadow.map((item) => item.decision).join(",") === "would-allow,would-deny,would-confirm,would-confirm",
  "Guard shadow records only would-allow / would-deny / would-confirm without resolving todos",
);
check(
  observedShadow.every((item) => /^[0-9a-f]{64}$/.test(item.actionFingerprint))
    && !JSON.stringify(observedShadow).includes("C:\\secret\\a.txt"),
  "Guard shadow uses 64-hex fingerprint and leaks no raw command parameters",
);

// 4) Throwing observer never breaks Guard
const throwingGuard = createGuard(mockStore, defaults, undefined, {
  permissionShadowEnabled: true,
  observePermissionShadow: () => {
    throw new Error("shadow failure");
  },
});
const safeRes = throwingGuard.beforeToolCall(
  { toolName: "get_weather", params: { city: "Shanghai" } },
  { sessionKey: "agent:main:main", runId: "run-2", trigger: "user" },
);
check(safeRes === undefined, "throwing shadow observer never affects Guard hook return");
console.log("ALL PASS core-bridge.test");
