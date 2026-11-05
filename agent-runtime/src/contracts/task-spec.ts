/**
 * TaskSpec 及嵌套 DTO 的 Zod 镜像,线协议以
 * `src/guanjia/core/contracts.py::task_spec_to_dict / task_spec_from_dict` 为准。
 *
 * 严格性对齐:
 * - 顶层 10 个键全部必填(Python `value["..."]` 直接取键,缺失即 KeyError);
 * - 嵌套对象用 `.strict()` 拒绝未知键(对应 Python `**kwargs` 展开)
 *   但带默认值的字段允许缺省,与 Python dataclass 默认值一致;
 * - `permission_scope` 键必填、值可为 null(nullable 而非 optional,与 Python 一致);
 * - `sha256` 大小写 hex 均可(Python 校验前先 `lower()`),与
 *   `AuthorizationRequest.action_fingerprint`(必须全小写)不同。
 */
import { z } from "zod";
import { identifierSchema } from "./ids.js";

/** 镜像 `core/contracts.py::ArtifactRef`。 */
export const artifactRefWireSchema = z
  .object({
    uri: identifierSchema,
    kind: identifierSchema,
    sha256: z
      .string()
      .regex(/^[0-9a-f]{64}$/i, { message: "sha256 must be a 64-character hexadecimal digest" })
      .nullable()
      .optional(),
    media_type: identifierSchema.nullable().optional(),
  })
  .strict();

export type ArtifactRefWire = z.infer<typeof artifactRefWireSchema>;

/** 领域对象:可选字段归一为 `| null`(与 Python dataclass 字段类型一致)。 */
export interface ArtifactRef {
  uri: string;
  kind: string;
  sha256: string | null;
  media_type: string | null;
}

export function artifactRefFromWire(wire: ArtifactRefWire): ArtifactRef {
  return { uri: wire.uri, kind: wire.kind, sha256: wire.sha256 ?? null, media_type: wire.media_type ?? null };
}

export function artifactRefToDict(ref: ArtifactRef): ArtifactRefWire {
  // Python 序列化用 `artifact.__dict__.copy()`,四个键始终全部输出(含 null)。
  return { uri: ref.uri, kind: ref.kind, sha256: ref.sha256, media_type: ref.media_type };
}

/** 镜像 `core/contracts.py::PermissionScope`。 */
export const permissionScopeWireSchema = z
  .object({
    purpose: identifierSchema,
    target_scope: z.string().optional(),
    risk_scope: z.string().optional(),
    grants: z.array(identifierSchema).optional(),
  })
  .strict();

export type PermissionScopeWire = z.infer<typeof permissionScopeWireSchema>;

export interface PermissionScope {
  purpose: string;
  target_scope: string;
  risk_scope: string;
  grants: string[];
}

export function permissionScopeFromWire(wire: PermissionScopeWire): PermissionScope {
  return {
    purpose: wire.purpose,
    target_scope: wire.target_scope ?? "",
    risk_scope: wire.risk_scope ?? "",
    grants: wire.grants ?? [],
  };
}

/** 镜像 `core/contracts.py::DeliveryRequirements`(注意 `allow_delay` 默认 true)。 */
export const deliveryRequirementsWireSchema = z
  .object({
    channels: z.array(identifierSchema).optional(),
    must_be_visible: z.boolean().optional(),
    allow_delay: z.boolean().optional(),
    format_hint: identifierSchema.nullable().optional(),
  })
  .strict();

export type DeliveryRequirementsWire = z.infer<typeof deliveryRequirementsWireSchema>;

export interface DeliveryRequirements {
  channels: string[];
  must_be_visible: boolean;
  allow_delay: boolean;
  format_hint: string | null;
}

export function deliveryRequirementsFromWire(wire: DeliveryRequirementsWire): DeliveryRequirements {
  return {
    channels: wire.channels ?? [],
    must_be_visible: wire.must_be_visible ?? false,
    allow_delay: wire.allow_delay ?? true,
    format_hint: wire.format_hint ?? null,
  };
}

/** 镜像 `core/contracts.py::ResourceRequest`。 */
export const resourceRequestWireSchema = z
  .object({
    resource: identifierSchema,
    mode: z.enum(["shared", "exclusive"]).optional(),
    units: z.number().int().min(1).optional(),
    release_on_pause: z.boolean().optional(),
  })
  .strict();

export type ResourceRequestWire = z.infer<typeof resourceRequestWireSchema>;

export interface ResourceRequest {
  resource: string;
  mode: "shared" | "exclusive";
  units: number;
  release_on_pause: boolean;
}

export function resourceRequestFromWire(wire: ResourceRequestWire): ResourceRequest {
  return {
    resource: wire.resource,
    mode: wire.mode ?? "shared",
    units: wire.units ?? 1,
    release_on_pause: wire.release_on_pause ?? false,
  };
}

/**
 * TaskSpec 线协议(顶层全键必填、`.strict()`)。
 * 跨字段规则:同一资源只能被请求一次(镜像 Python
 * `a task may request each resource only once`)。
 */
export const taskSpecWireSchema = z
  .object({
    task_id: identifierSchema,
    revision: z.number().int().min(1),
    goal: identifierSchema,
    constraints: z.array(identifierSchema),
    references: z.array(artifactRefWireSchema),
    permission_scope: permissionScopeWireSchema.nullable(),
    priority: z.number().int(),
    success_criteria: z.array(identifierSchema),
    delivery_requirements: deliveryRequirementsWireSchema,
    resources: z.array(resourceRequestWireSchema),
  })
  .strict()
  .superRefine((wire, ctx) => {
    const seen = new Set<string>();
    for (const request of wire.resources) {
      if (seen.has(request.resource)) {
        ctx.addIssue({
          code: "custom",
          path: ["resources"],
          message: "a task may request each resource only once",
        });
      }
      seen.add(request.resource);
    }
  });

export type TaskSpecWire = z.infer<typeof taskSpecWireSchema>;

export const taskSpecSchema = taskSpecWireSchema.transform((wire) => ({
  task_id: wire.task_id,
  revision: wire.revision,
  goal: wire.goal,
  constraints: wire.constraints,
  references: wire.references.map(artifactRefFromWire),
  permission_scope: wire.permission_scope === null ? null : permissionScopeFromWire(wire.permission_scope),
  priority: wire.priority,
  success_criteria: wire.success_criteria,
  delivery_requirements: deliveryRequirementsFromWire(wire.delivery_requirements),
  resources: wire.resources.map(resourceRequestFromWire),
}));

export type TaskSpec = z.infer<typeof taskSpecSchema>;

/** 镜像 `task_spec_from_dict`。 */
export function taskSpecFromDict(value: unknown): TaskSpec {
  return taskSpecSchema.parse(value);
}

/** 镜像 `task_spec_to_dict`:键集合与 Python 输出逐字段一致。 */
export function taskSpecToDict(spec: TaskSpec): TaskSpecWire {
  return {
    task_id: spec.task_id,
    revision: spec.revision,
    goal: spec.goal,
    constraints: [...spec.constraints],
    references: spec.references.map(artifactRefToDict),
    permission_scope:
      spec.permission_scope === null
        ? null
        : {
            purpose: spec.permission_scope.purpose,
            target_scope: spec.permission_scope.target_scope,
            risk_scope: spec.permission_scope.risk_scope,
            grants: [...spec.permission_scope.grants],
          },
    priority: spec.priority,
    success_criteria: [...spec.success_criteria],
    delivery_requirements: {
      channels: [...spec.delivery_requirements.channels],
      must_be_visible: spec.delivery_requirements.must_be_visible,
      allow_delay: spec.delivery_requirements.allow_delay,
      format_hint: spec.delivery_requirements.format_hint,
    },
    resources: spec.resources.map((request) => ({
      resource: request.resource,
      mode: request.mode,
      units: request.units,
      release_on_pause: request.release_on_pause,
    })),
  };
}
