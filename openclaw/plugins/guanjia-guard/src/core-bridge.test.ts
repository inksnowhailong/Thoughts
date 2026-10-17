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
console.log("ALL PASS core-bridge.test");
