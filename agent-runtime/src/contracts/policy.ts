/**
 * 权限域契约的 Zod 镜像,对齐 `src/guanjia/permission/contracts.py`:
 * - `Role`(roles.py)、`Reversibility`、`PermissionOutcome` 枚举;
 * - `ActorEvidence`(sources 必须非空);
 * - `AuthorizationRequest`(`action_fingerprint` 必须 64 位全小写 hex,
 *   注意:Python 用 `re.fullmatch(r'[0-9a-f]{64}')` 且不 lower,大写会被拒绝,
 *   这与 `ArtifactRef.sha256`(大小写均可)不同);
 * - `PermissionDecision` 的 todo_id 条件规则用 `superRefine` 实现:
 *   allow/deny 必须无 todo_id;confirmation_required 必须有 todo_id。
 */
import { z } from "zod";
import { formatUtc, isoUtcSchema, nonEmptyIdentifierSchema, parseUtc } from "./ids.js";

/** 镜像 `permission/roles.py::Role`。 */
export const roleSchema = z.enum(["owner", "trusted_family", "visitor", "self_loop"]);

/** 镜像 `permission/contracts.py::Reversibility`。 */
export const reversibilitySchema = z.enum(["reversible", "irreversible"]);

/** 镜像 `permission/contracts.py::PermissionOutcome`。 */
export const permissionOutcomeSchema = z.enum(["allow", "deny", "confirmation_required"]);

/** 镜像 `permission/contracts.py::ActorEvidence`(必填字段用 `required()` = strip 非空)。 */
export const actorEvidenceWireSchema = z
  .object({
    actor_id: nonEmptyIdentifierSchema,
    role: roleSchema,
    sources: z.array(nonEmptyIdentifierSchema).min(1),
  })
  .strict();

export type ActorEvidenceWire = z.infer<typeof actorEvidenceWireSchema>;

export interface ActorEvidence {
  actor_id: string;
  role: z.infer<typeof roleSchema>;
  sources: string[];
}

export const actorEvidenceSchema = actorEvidenceWireSchema.transform((wire) => ({
  actor_id: wire.actor_id,
  role: wire.role,
  sources: [...wire.sources],
}));

/** 镜像 `permission/contracts.py::AuthorizationRequest`。 */
export const authorizationRequestWireSchema = z
  .object({
    request_id: nonEmptyIdentifierSchema,
    actor: actorEvidenceWireSchema,
    session_id: nonEmptyIdentifierSchema,
    task_id: nonEmptyIdentifierSchema.nullable(),
    intent: nonEmptyIdentifierSchema,
    capability: nonEmptyIdentifierSchema,
    object_scope: nonEmptyIdentifierSchema,
    risk_scope: nonEmptyIdentifierSchema,
    within_intent_scope: z.boolean(),
    reversibility: reversibilitySchema,
    provenance_summary: nonEmptyIdentifierSchema,
    now: isoUtcSchema,
    action_fingerprint: z.string().regex(/^[0-9a-f]{64}$/, {
      message: "action_fingerprint must be a lowercase 64-hex string",
    }),
  })
  .strict();

export type AuthorizationRequestWire = z.infer<typeof authorizationRequestWireSchema>;

export const authorizationRequestSchema = authorizationRequestWireSchema.transform((wire) => ({
  request_id: wire.request_id,
  actor: actorEvidenceSchema.parse(wire.actor),
  session_id: wire.session_id,
  task_id: wire.task_id,
  intent: wire.intent,
  capability: wire.capability,
  object_scope: wire.object_scope,
  risk_scope: wire.risk_scope,
  within_intent_scope: wire.within_intent_scope,
  reversibility: wire.reversibility,
  provenance_summary: wire.provenance_summary,
  now: parseUtc(wire.now),
  action_fingerprint: wire.action_fingerprint,
}));

export type AuthorizationRequest = z.infer<typeof authorizationRequestSchema>;

export function authorizationRequestFromDict(value: unknown): AuthorizationRequest {
  return authorizationRequestSchema.parse(value);
}

export function authorizationRequestToDict(request: AuthorizationRequest): AuthorizationRequestWire {
  return {
    request_id: request.request_id,
    actor: {
      actor_id: request.actor.actor_id,
      role: request.actor.role,
      sources: [...request.actor.sources],
    },
    session_id: request.session_id,
    task_id: request.task_id,
    intent: request.intent,
    capability: request.capability,
    object_scope: request.object_scope,
    risk_scope: request.risk_scope,
    within_intent_scope: request.within_intent_scope,
    reversibility: request.reversibility,
    provenance_summary: request.provenance_summary,
    now: formatUtc(request.now),
    action_fingerprint: request.action_fingerprint,
  };
}

/**
 * 镜像 `permission/contracts.py::PermissionDecision`。
 * todo_id 条件规则(与 Python 逐条一致):
 * - outcome ∈ {allow, deny} 且 todo_id 非空 → 拒绝;
 * - outcome == confirmation_required 且 todo_id 缺省/空 → 拒绝。
 */
export const permissionDecisionWireSchema = z
  .object({
    outcome: permissionOutcomeSchema,
    reason: nonEmptyIdentifierSchema,
    todo_id: nonEmptyIdentifierSchema.nullable().optional(),
  })
  .strict()
  .superRefine((decision, ctx) => {
    const hasTodo = decision.todo_id !== undefined && decision.todo_id !== null;
    if (decision.outcome === "allow" || decision.outcome === "deny") {
      if (hasTodo) {
        ctx.addIssue({
          code: "custom",
          path: ["todo_id"],
          message: "allow and deny decisions cannot include a todo_id",
        });
      }
    } else if (!hasTodo) {
      ctx.addIssue({
        code: "custom",
        path: ["todo_id"],
        message: "confirmation decisions require a todo_id",
      });
    }
  });

export type PermissionDecisionWire = z.infer<typeof permissionDecisionWireSchema>;

export const permissionDecisionSchema = permissionDecisionWireSchema.transform((wire) => ({
  outcome: wire.outcome,
  reason: wire.reason,
  todo_id: wire.todo_id ?? null,
}));

export type PermissionDecision = z.infer<typeof permissionDecisionSchema>;

export function permissionDecisionFromDict(value: unknown): PermissionDecision {
  return permissionDecisionSchema.parse(value);
}

export function permissionDecisionToDict(decision: PermissionDecision): PermissionDecisionWire {
  return {
    outcome: decision.outcome,
    reason: decision.reason,
    todo_id: decision.todo_id,
  };
}
