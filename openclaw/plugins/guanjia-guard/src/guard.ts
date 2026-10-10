import crypto from "node:crypto";
import { classify, resolveToolCallTarget, type Classification, type Level, type LevelsFile } from "./levels";
import type { Store } from "./store";
import { stripVoiceTag } from "./speak";

/**
 * 分级中间层（D6）：before_tool_call 里定级并放行 / 拦截 / 走审批。
 *
 *   L0/L1 ──> 放行
 *   L2    ──> 拦下，让模型先问一句；用户下一句说"确认"后，同参数重试放行一次
 *   L3    ──> requireApproval，走 approvals.plugin 推到微信 + 桌面 Control UI
 *   cron/心跳会话：先降一档再按上面处理；L2 在主动会话里无人可答，直接拦
 *
 * 结构化工具搜索下，一次 tool_call 会触发两次本钩子，只在外层定级一次：
 *
 *   外层 tool_call{id,args} ──> 还原真实工具名 + 参数后定级（唯一定级点）
 *        │ 放行 / L2 确认后放行 / L3 批准  ──> 按外层 toolCallId 登记
 *        ▼
 *   内层真实工具，toolCallId = tool_search_code:<外层id>:<工具名>:<序号>
 *        ├─ 有登记且工具名一致 ──> 放行并消费登记（不二次确认）
 *        └─ 没有登记（代码模式等）──> 按真实工具正常定级
 *
 *   client: 来源的目录工具 OpenClaw 不包内层钩子，外层直接按 L3 走带外审批
 */

/** 等待口头确认的 L2 动作（进程内存即可，3 分钟过期，网关重启丢了也只是要重问） */
interface PendingAction {
  sessionKey: string;
  fingerprint: string;
  summary: string;
  expiresAt: number;
  approved: boolean;
}

/** 外层 tool_call 放行后的登记，内层钩子凭它认出"同一次调用" */
interface OuterGrant {
  toolName: string;
  fingerprint: string;
  level: Level;
  sessionKey?: string;
  expiresAt: number;
}

const PENDING_TTL_MS = 3 * 60_000;
/** 外层放行或批准后才登记，60 秒内参数一致的内层调用去重一次 */
const OUTER_GRANT_TTL_MS = 60_000;
/** L3 带外审批等待时长；OpenClaw 默认 120 秒、上限 600 秒 */
export const L3_APPROVAL_TIMEOUT_MS = 120_000;
const CONFIRM_RE = /^(确认|确定|好|好的|可以|行|执行|同意|是的|对|嗯|ok|yes)/i;

/** 决定"是不是同一件事"的目标参数，按优先级取第一个存在的 */
const TARGET_KEYS = ["command", "path", "file_path", "url", "to", "action"];

/**
 * 同一工具 + 同一操作目标 → 同一指纹，确认只对这件事生效。
 * 不用全部参数：模型重试时常有无关差异（多个换行、多带一个可选参数），全量比对会让确认永远对不上。
 * 目标做归一化：小写、斜杠统一、去首尾空白。没有目标参数的工具退回全量参数比对。
 */
const fingerprintOf = (toolName: string, params: Record<string, unknown>) => {
  const key = TARGET_KEYS.find((k) => typeof params[k] === "string");
  const target = key ? `${key}=${String(params[key]).trim().toLowerCase().replace(/\\/g, "/")}` : stableJson(params);
  return crypto.createHash("sha256").update(toolName + "\u0000" + target).digest("hex").slice(0, 16);
};

function stableJson(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stableJson).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + stableJson((v as any)[k])).join(",")}}`;
  }
  return JSON.stringify(v);
}

/** 给人看的一行动作摘要（审批卡片 / 模型提问用），截断避免把大段参数带出去 */
function summarize(toolName: string, params: Record<string, unknown>): string {
  const main = params.command ?? params.path ?? params.file_path ?? params.url ?? params.action ?? params.to;
  const s = typeof main === "string" ? main : JSON.stringify(params);
  return `${toolName}: ${s.length > 120 ? s.slice(0, 117) + "..." : s}`;
}

/** 与 OpenClaw ToolSearchRuntime 的 sanitizeToolCallIdPart 一致：内层 toolCallId 里嵌的是清洗后的外层 id */
export function sanitizeToolCallIdPart(value: string): string {
  return value.trim().replace(/[^A-Za-z0-9_.:-]+/g, "_").slice(0, 120) || "call";
}

/** 从内层 toolCallId 取外层 id；不是 tool_search_code:<外层id>:<工具名>:<序号> 形状时返回 undefined */
export function parentToolCallIdOf(toolCallId: unknown): string | undefined {
  if (typeof toolCallId !== "string") return undefined;
  return /^tool_search_code:(.*):[^:]+:\d+$/.exec(toolCallId)?.[1];
}

export function createGuard(store: Store, defaults: LevelsFile) {
  const pending = new Map<string, PendingAction>();
  /** runId → 触发来源（user/cron/heartbeat），由 prompt 阶段钩子登记；工具钩子上下文里没有 trigger */
  const runTriggers = new Map<string, string>();
  /** 清洗后的外层 toolCallId → 放行登记，只保留最近 500 条 */
  const outerGrants = new Map<string, OuterGrant>();

  /** 记录一次运行的触发来源，只保留最近 200 条 */
  function noteRun(runId: string | undefined, trigger: string | undefined) {
    if (!runId || !trigger) return;
    runTriggers.set(runId, trigger);
    if (runTriggers.size > 200) runTriggers.delete(runTriggers.keys().next().value!);
  }

  /** 是否主动发起：cron/心跳触发，或会话 key 落在 cron 命名空间 */
  function isProactive(runId?: string, sessionKey?: string) {
    const t = runId ? runTriggers.get(runId) : undefined;
    return t === "cron" || t === "heartbeat" || /:cron:/.test(sessionKey ?? "");
  }

  /**
   * 用户新开口时调用：命中"确认"就把该会话最近一个待确认动作标记为已批准。
   * @returns 给模型的追加上下文
   */
  function onUserTurn(sessionKey: string | undefined, userText: string): string | undefined {
    if (!sessionKey) return undefined;
    const p = pending.get(sessionKey);
    if (!p || p.expiresAt < Date.now()) return undefined;
    if (!CONFIRM_RE.test(stripVoiceTag(userText).trim())) {
      pending.delete(sessionKey);
      store.audit({ kind: "tool", decision: "l2-dropped", sessionKey, summary: p.summary });
      return undefined;
    }
    p.approved = true;
    p.expiresAt = Date.now() + PENDING_TTL_MS;
    store.audit({ kind: "tool", decision: "l2-confirmed", sessionKey, summary: p.summary });
    return `（系统：用户已口头确认刚才待确认的操作「${p.summary}」。现在用与刚才完全相同的参数再调用一次该工具。）`;
  }

  /** 外层 tool_call 放行时登记；外层没有 toolCallId 时 OpenClaw 用 "direct" 作父 id */
  function grantOuter(callId: string | undefined, grant: Omit<OuterGrant, "expiresAt">) {
    outerGrants.set(sanitizeToolCallIdPart(callId ?? "direct"), { ...grant, expiresAt: Date.now() + OUTER_GRANT_TTL_MS });
    if (outerGrants.size > 500) outerGrants.delete(outerGrants.keys().next().value!);
  }

  /** 内层触发时取走对应登记（只能用一次）；对不上或已过期返回 undefined */
  function takeOuterGrant(callId: unknown, toolName: string, params: Record<string, unknown>): OuterGrant | undefined {
    const parent = parentToolCallIdOf(callId);
    if (parent === undefined) return undefined;
    const grant = outerGrants.get(parent);
    if (!grant) return undefined;
    outerGrants.delete(parent);
    const same = grant.toolName === toolName && grant.fingerprint === fingerprintOf(toolName, params);
    return same && grant.expiresAt > Date.now() ? grant : undefined;
  }

  /** before_tool_call 处理器 */
  function beforeToolCall(event: any, ctx: any) {
    const rawParams = (event.params ?? {}) as Record<string, unknown>;
    const rawToolName: string = event.toolName;
    const callId: string | undefined = event.toolCallId ?? ctx?.toolCallId;

    const grant = takeOuterGrant(callId, rawToolName, rawParams);
    if (grant) {
      store.audit({
        kind: "tool",
        tool: rawToolName,
        level: grant.level,
        source: "outer-grant",
        sessionKey: ctx?.sessionKey ?? grant.sessionKey,
        summary: summarize(rawToolName, rawParams),
        paramKeys: Object.keys(rawParams).sort(),
        decision: "allow-inner-dedup",
      });
      return;
    }

    const levels = store.readJson<LevelsFile>("levels.json", defaults);
    const target = rawToolName === "tool_call" ? resolveToolCallTarget(rawParams) : undefined;
    const toolName = target?.toolName ?? rawToolName;
    const params = target?.params ?? rawParams;
    const sessionKey: string | undefined = ctx?.sessionKey;
    const proactive = isProactive(ctx?.runId ?? event.runId, sessionKey);
    // client 工具没有内层钩子；裸名字可能按名字匹配到 client 工具；选择器矛盾时真实目标不确定——三种都只能在外层按最严处理
    const forceL3 = target?.source === "client" || target?.source === "ambiguous" || (!!target && target.source === undefined && !levels.tools[target.toolName]);
    const c: Classification = forceL3
      ? { level: "L3", source: "client", note: "tool_call 目标可能是 client 来源或无法确定，一律带外审批", downgraded: false }
      : classify(levels, { toolName, params, derivedPaths: event.derivedPaths, sessionKey }, proactive);
    const summary = summarize(toolName, params);
    const base = {
      kind: "tool",
      tool: toolName,
      level: c.level,
      source: c.source,
      proactive,
      sessionKey,
      summary,
      paramKeys: Object.keys(params).sort(),
      ...(target ? { via: "tool_call", catalogSource: target.source ?? "bare" } : {}),
    };
    /** 只有外层 tool_call 需要登记；直接调用的工具不会再触发内层 */
    const grantIfOuter = () => {
      if (target) grantOuter(callId, { toolName, fingerprint: fingerprintOf(toolName, params), level: c.level, sessionKey });
    };

    if (c.level === "L0" || c.level === "L1") {
      grantIfOuter();
      store.audit({ ...base, decision: "allow" });
      return;
    }

    if (c.level === "L2") {
      const fp = fingerprintOf(toolName, params);
      const p = sessionKey ? pending.get(sessionKey) : undefined;
      if (p && p.approved && p.fingerprint === fp && p.expiresAt > Date.now()) {
        pending.delete(sessionKey!);
        grantIfOuter();
        store.audit({ ...base, decision: "allow-confirmed" });
        return;
      }
      if (proactive || !sessionKey) {
        store.audit({ ...base, decision: "block-no-confirmer" });
        return { block: true, blockReason: `需要确认：${summary}。这是主动发起的会话，没人能当场确认，已拦下。不要重试，等用户在场时再提。` };
      }
      pending.set(sessionKey, { sessionKey, fingerprint: fp, summary, expiresAt: Date.now() + PENDING_TTL_MS, approved: false });
      store.audit({ ...base, decision: "block-need-confirm" });
      return {
        block: true,
        blockReason: `需要确认：${summary}。先用一句话问用户要不要这么做；用户说"确认"后，用完全相同的参数再调用一次。`,
      };
    }

    // L3：带外审批（手机微信 + 桌面 Control UI）
    store.audit({ ...base, decision: "approval-requested" });
    return {
      requireApproval: {
        title: `管家要执行高风险操作（${toolName}）`.slice(0, 80),
        description: `${summary}${c.note ? `\n原因：${c.note}` : ""}${proactive ? "\n（主动发起，已降档）" : ""}`.slice(0, 512),
        severity: "critical",
        allowedDecisions: ["allow-once", "deny"],
        timeoutMs: L3_APPROVAL_TIMEOUT_MS,
        onResolution: (decision: string) => {
          if (decision === "allow-once") grantIfOuter();
          store.audit({ ...base, decision: `approval-${decision}` });
        },
      },
    };
  }

  return { beforeToolCall, onUserTurn, noteRun, isProactive };
}
