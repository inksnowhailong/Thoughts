/**
 * Pure contracts for Wave 2 shadow comparison.  This file deliberately has
 * no imports, hooks, I/O, registration, or permission-release capability.
 */

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type ShadowKind = "reply" | "task_notice" | "permission_request";

export interface OutputRequestView {
  outputId: string;
  actorId: string;
  conversationId: string;
  text: string | null;
  channels: readonly string[];
  taskId?: string | null;
}

export interface PermissionDecisionView {
  outcome: "allow" | "deny" | "confirmation_required";
  todoId: string | null;
}

export interface LegacyObservation {
  readonly [key: string]: JsonValue;
}

export interface ShadowDifference {
  field: string;
  expected: JsonValue | undefined;
  observed: JsonValue | undefined;
}

export interface ShadowComparison {
  kind: ShadowKind;
  eventKey: string;
  expected: LegacyObservation;
  observed: LegacyObservation;
  differences: readonly ShadowDifference[];
}

export interface PermissionAuditView {
  todoId: string;
  actorId: string;
  sessionId: string;
  taskId: string | null;
  actionFingerprint: string;
  outcome: PermissionDecisionView["outcome"];
  recordedAtUtc: string;
  displayExpiresAtUtc: string;
  latchExpiresAtUtc: string;
}

export const UI_DISPLAY_TTL_MS = 3 * 60_000;
export const LATCH_TTL_MS = 10 * 60_000;

const MAX_DEPTH = 8;
const MAX_ITEMS = 64;
const MAX_STRING = 2_048;
const TIMESTAMP_LIMIT = 8.64e15;
const sensitive = /secret|token|password|credential|api_?key|target|command|param|path|file|media|artifact|object_scope/i;
const dangerousKey = /^(?:__proto__|prototype|constructor)$/;

function checkedJson(value: unknown, depth = 0): JsonValue {
  if (depth > MAX_DEPTH) throw new TypeError("JSON exceeds maximum depth");
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    if (typeof value === "string" && value.length > MAX_STRING) throw new TypeError("JSON string exceeds maximum length");
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("JSON number must be finite");
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_ITEMS) throw new TypeError("JSON array exceeds maximum length");
    return value.map((item) => checkedJson(item, depth + 1));
  }
  if (typeof value !== "object" || Object.getPrototypeOf(value) !== Object.prototype) {
    throw new TypeError("value must be a plain JSON object");
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length > MAX_ITEMS || entries.some(([key]) => dangerousKey.test(key))) {
    throw new TypeError("JSON object contains too many or unsafe keys");
  }
  return Object.fromEntries(entries.map(([key, item]) => [key, checkedJson(item, depth + 1)]));
}

/** Canonical JSON validates runtime input before creating a deterministic value. */
export function canonicalJson(value: unknown): string {
  const checked = checkedJson(value);
  if (checked === null || typeof checked !== "object") return JSON.stringify(checked);
  if (Array.isArray(checked)) return `[${checked.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(checked).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(checked[key])}`).join(",")}}`;
}

/** Redact sensitive content recursively after rejecting unsafe runtime values. */
export function redact(value: unknown, key = "", depth = 0): JsonValue {
  const checked = checkedJson(value, depth);
  if (sensitive.test(key)) return "<redacted>";
  if (typeof checked === "string") {
    const safe = checked
      .replace(/\bBearer\s+[A-Za-z0-9\-._~+/]+=*/gi, "Bearer <redacted>")
      .replace(/\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b/gi, "<redacted:key>")
      .replace(/([?&](?:token|access_token|api_?key|key|signature|password|secret)=)[^&#\s]*/gi, "$1<redacted>")
      .replace(/\b(password|secret|token|api_?key)\s*([:=])\s*([^\s,;]+)/gi, "$1$2<redacted>")
      .replace(/(?:file:(?:\/\/)?[^\s'"]+|\\\\[^\s'"]+|[A-Za-z]:[\\/][^\s'"]*|(?<!\w)\/(?:[^\s'"]+))/gi, "<redacted:path>")
      .replace(/\b(?:media|artifact):\/\/[^\s'"]+|\b[^\s'"]+\.(?:mp3|mp4|wav|m4a|webm|jpg|jpeg|png|gif|webp)\b/gi, "<redacted:media>");
    return safe;
  }
  if (Array.isArray(checked)) return checked.map((item) => redact(item, key, depth + 1));
  if (checked !== null && typeof checked === "object") {
    return Object.fromEntries(Object.entries(checked).map(([name, item]) => [name, redact(item, name, depth + 1)]));
  }
  return checked;
}

/** Derive only a deterministic correlation key; it cannot authorize anything. */
export function shadowEventKey(kind: ShadowKind, stableId: string): string {
  if (!stableId.trim()) throw new TypeError("stableId is required");
  return `shadow:${kind}:${stableId}`;
}

/** Compare the exact projected fields and preserve a structured, redacted diff. */
export function compareShadow(
  kind: ShadowKind,
  stableId: string,
  expected: LegacyObservation,
  observed: LegacyObservation,
): ShadowComparison {
  const differences: ShadowDifference[] = [];
  for (const field of [...new Set([...Object.keys(expected), ...Object.keys(observed)])].sort()) {
    const hasExpected = Object.hasOwn(expected, field);
    const hasObserved = Object.hasOwn(observed, field);
    const left = hasExpected ? expected[field] : undefined;
    const right = hasObserved ? observed[field] : undefined;
    if (hasExpected !== hasObserved || (hasExpected && hasObserved && canonicalJson(expected[field]) !== canonicalJson(observed[field]))) {
      differences.push({
        field,
        expected: hasExpected ? redact(left, field) : undefined,
        observed: hasObserved ? redact(right, field) : undefined,
      });
    }
  }
  return {
    kind,
    eventKey: shadowEventKey(kind, stableId),
    expected: redact(expected) as LegacyObservation,
    observed: redact(observed) as LegacyObservation,
    differences,
  };
}

/** Build the passive projection for a legacy reply or task-notice observation. */
export function outputExpectation(kind: "reply" | "task_notice", request: OutputRequestView): LegacyObservation {
  return {
    kind,
    output_id: request.outputId,
    actor_id: request.actorId,
    conversation_id: request.conversationId,
    task_id: request.taskId ?? null,
    text: request.text,
    channels: [...request.channels],
  };
}

/** Validate the opaque SHA-256 action fingerprint owned by the core permission layer. */
export function opaqueActionFingerprint(value: string): string {
  if (!/^[a-f0-9]{64}$/i.test(value)) throw new TypeError("actionFingerprint must be an opaque SHA-256 hex value");
  return value.toLowerCase();
}

/** UTC-only timestamp construction with independent 3m UI and 10m latch fields. */
export function permissionAuditView(
  input: Omit<PermissionAuditView, "recordedAtUtc" | "displayExpiresAtUtc" | "latchExpiresAtUtc">,
  nowMs: number,
): PermissionAuditView {
  if (!Number.isFinite(nowMs) || Math.abs(nowMs) > TIMESTAMP_LIMIT || Math.abs(nowMs + LATCH_TTL_MS) > TIMESTAMP_LIMIT) {
    throw new TypeError("nowMs must be within the JavaScript TimeClip range");
  }
  if (!input.todoId || !input.actorId || !input.sessionId || !input.actionFingerprint) throw new TypeError("confirmation binding is incomplete");
  return {
    ...input,
    actionFingerprint: opaqueActionFingerprint(input.actionFingerprint),
    recordedAtUtc: new Date(nowMs).toISOString(),
    displayExpiresAtUtc: new Date(nowMs + UI_DISPLAY_TTL_MS).toISOString(),
    latchExpiresAtUtc: new Date(nowMs + LATCH_TTL_MS).toISOString(),
  };
}

/** Exact-scope lookup predicate; it neither approves nor consumes a latch. */
export function matchesConfirmation(
  record: PermissionAuditView,
  query: Pick<PermissionAuditView, "todoId" | "actorId" | "sessionId" | "taskId" | "actionFingerprint">,
  nowMs: number,
): boolean {
  if (!Number.isFinite(nowMs) || Math.abs(nowMs) > TIMESTAMP_LIMIT) throw new TypeError("nowMs must be within the JavaScript TimeClip range");
  const latchAt = strictUtcMillis(record.latchExpiresAtUtc, "record.latchExpiresAtUtc");
  return record.todoId === query.todoId
    && record.actorId === query.actorId
    && record.sessionId === query.sessionId
    && record.taskId === query.taskId
    && opaqueActionFingerprint(record.actionFingerprint) === opaqueActionFingerprint(query.actionFingerprint)
    && nowMs < latchAt;
}

function strictUtcMillis(value: unknown, field: string): number {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value)) {
    throw new TypeError(`${field} must be a canonical UTC timestamp`);
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || Math.abs(parsed) > TIMESTAMP_LIMIT || new Date(parsed).toISOString() !== value) {
    throw new TypeError(`${field} must be a valid UTC timestamp`);
  }
  return parsed;
}
