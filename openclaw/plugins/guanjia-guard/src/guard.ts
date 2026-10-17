import crypto from "node:crypto";
import { classify, resolveToolCallTarget, type AskCategory, type Classification, type Level, type LevelsFile } from "./levels.ts";
import type { Store } from "./store.ts";
import { hasVoiceTag, stripVoiceTag } from "./speak.ts";
import { CATEGORY_CN, appendOverride, buildRememberedOverride } from "./remember.ts";
import { APPROVAL_RETRY_MARKER, createApprovalLatches } from "./approvals.ts";
import { consumeOwnerWechatProvenance } from "./wechat-provenance.ts";
import { evaluateShadowPermission, type ShadowPermissionObservation } from "./core-bridge.ts";

/**
 * 分级中间层（确认策略 v2）：before_tool_call 里定级并放行 / 先问 / 拒绝。
 *
 *   L0/L1 ──> 直接做
 *   L2    ──> 拦下，让模型先口头问一句；用户下一句说"允许/可以/行…"后，同参数重试放行一次；
 *             说"以后都允许"还会把这件事记进线上 levels.json 的 overrides（读密钥、改管家规则两类不记）
 *   L3    ──> 拒绝，不审批：tool_call 指向的真实工具无法确定（client 来源 / 选择器矛盾 / 不在分级表里的裸名字）；
 *             分级表里手写的 L3 按 L2 先问、不可记住
 *   cron/心跳会话：L2 没人能答，直接拦下并写审计
 *
 * 结构化工具搜索下，一次 tool_call 会触发两次本钩子，只在外层定级一次：
 *
 *   外层 tool_call{id,args} ──> 还原真实工具名 + 参数后定级（唯一定级点）
 *        │ 放行 / L2 确认后放行  ──> 按外层 toolCallId 登记
 *        ▼
 *   内层真实工具，toolCallId = tool_search_code:<外层id>:<工具名>:<序号>
 *        ├─ 有登记且工具名一致 ──> 放行并消费登记（不二次确认）
 *        └─ 没有登记（代码模式等）──> 按真实工具正常定级
 */

/** 等待口头确认的 L2 动作（进程内存即可，3 分钟过期，网关重启丢了也只是要重问） */
interface PendingAction {
  sessionKey: string;
  fingerprint: string;
  summary: string;
  toolName: string;
  params: Record<string, unknown>;
  category?: AskCategory;
  rememberable: boolean;
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
/** 外层放行后才登记，60 秒内参数一致的内层调用去重一次 */
const OUTER_GRANT_TTL_MS = 60_000;
const APPROVE_SHORT = new Set(["允许","确认","确定","可以","行","好","好的","好呀","好啊","嗯","对","同意","没问题","去吧","弄吧","做吧","执行","执行吧","关吧","删吧","装吧","开吧","放行"]);
const DENY_RE = /不|别|算了|取消|暂停|等等|等一下|停|慢着|吗|\?|？/;
/** "以后都允许 / 一直允许 / 以后不用问了"：同意并记住；前面可以先带一个同意词 */
const REMEMBER_RE =
  /^(?:嗯|啊|哦|呃)?[，,。\s]*(?:(?:好的?|可以|行|允许|同意|确认|对)[，,。!！\s]*)?(?:(?:以后|今后|往后|之后)(?:就|也|都)*|一直|永远|总是)[，,\s]*(?:允许|可以|同意|放行|行|直接(?:做|弄|执行)?|(?:不用|别|不要|无需|甭)(?:再)?问)/;
/** 记住类的话里出现这些就不算：否定、叫停、反问（"不用问""别问"本身不算否定） */
const REMEMBER_DENY_RE = /算了|取消|等等|等一下|停|吗|\?|？|不(?:允许|可以|行|同意|要(?!再?问))|别(?!再?问)|不用(?!再?问)/;

export function isConfirmation(userText: string): boolean {
  const t = stripVoiceTag(userText).trim();
  const normalized = t.replace(/[\s，,。.!！?？~～、】【、]/g, "");
  if (isRememberRequest(t) || DENY_RE.test(normalized) || normalized.length > 12) return isRememberRequest(t);
  if (normalized.startsWith("好了好了") || normalized.startsWith("好家伙")) return false;
  return APPROVE_SHORT.has(normalized) || ["允许","确认","可以","行","好"].some((prefix) => normalized.startsWith(prefix));
}

export function isRememberRequest(userText: string): boolean {
  const t = stripVoiceTag(userText).trim();
  return REMEMBER_RE.test(t) && !REMEMBER_DENY_RE.test(t);
}

export function isRejection(userText: string): boolean {
  const t = stripVoiceTag(userText).trim().replace(/[\s，,。.!！?？~～、】【、]/g, "");
  return t.length <= 12 && (/^(?:不|不要|别|取消|算了|等等|等一下|暂停|停|慢着|不允许|不行)/.test(t));
}

/** 决定"是不是同一件事"的目标参数，按优先级取第一个存在的 */
const TARGET_KEYS = ["command", "path", "file_path", "url", "to", "action"];

/**
 * 同一工具 + 同一操作目标 → 同一指纹，确认只对这件事生效。
 * 不用全部参数：模型重试时常有无关差异（多个换行、多带一个可选参数），全量比对会让确认永远对不上。
 * 目标做归一化：小写、斜杠统一、去首尾空白。没有目标参数的工具退回全量参数比对。
 */
const actionHash64Of = (toolName: string, params: Record<string, unknown>) => {
  const key = TARGET_KEYS.find((k) => typeof params[k] === "string");
  const target = key ? `${key}=${String(params[key]).trim().toLowerCase().replace(/\\/g, "/")}` : stableJson(params);
  return crypto.createHash("sha256").update(toolName + "\u0000" + target).digest("hex");
};

const fingerprintOf = (toolName: string, params: Record<string, unknown>) => actionHash64Of(toolName, params).slice(0, 16);

function stableJson(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stableJson).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + stableJson((v as any)[k])).join(",")}}`;
  }
  return JSON.stringify(v);
}

/** 给人看的一行动作摘要（提问 / 审计用），截断避免把大段参数带出去 */
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

/** 记不住时给模型的解释 */
const REMEMBER_FAIL_CN: Record<string, string> = {
  not_exec: "这类操作没法精确记住",
  no_command: "这条命令是空的，没法记住",
  kill_without_name: "这次是按进程号结束的，没有程序名可记",
};

export function createGuard(
  store: Store,
  defaults: LevelsFile,
  onTaskApproval?: (input: { sessionKey?: string; approvalId: string; description: string; expiresAt: number }) => void,
  options?: {
    permissionShadowEnabled?: boolean;
    observePermissionShadow?: (observation: ShadowPermissionObservation) => void;
  },
) {
  const pending = new Map<string, PendingAction>();
  const approvals = createApprovalLatches(store);
  const runSources = new Map<string, string>();
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
   * 用户新开口时调用：同意就把该会话最近一个待确认动作标记为已批准；"以后都允许"再记进 overrides。
   * @param userText 用户原话（可带「[语音 …]」标记，这里剥）
   * @returns 给模型的追加上下文
   */
  function onUserTurn(sessionKey: string | undefined, userText: string, runId?: string): string | undefined {
    if (!sessionKey) return undefined;
    if (runId) {
      runSources.set(runId, hasVoiceTag(userText) ? "voice" : "user");
      if (runSources.size > 500) runSources.delete(runSources.keys().next().value!);
    }
    if (stripVoiceTag(userText).trim() === APPROVAL_RETRY_MARKER) return approvals.instruction(sessionKey);
    // O1-B 不再接受模型上下文里的口头确认：只能由本机 IPC 写入闩锁。
    if (isConfirmation(userText)) return undefined;
    const p = pending.get(sessionKey);
    if (!p || p.expiresAt < Date.now()) return undefined;
    const voice = hasVoiceTag(userText);
    const remember = isRememberRequest(userText);
    if (!remember && !isConfirmation(userText)) {
      pending.delete(sessionKey);
      store.audit({ kind: "tool", decision: "l2-dropped", sessionKey, summary: p.summary, voice });
      return undefined;
    }
    p.approved = true;
    p.expiresAt = Date.now() + PENDING_TTL_MS;
    const retry = "现在用与刚才完全相同的参数再调用一次该工具。";
    if (!remember) {
      store.audit({ kind: "tool", decision: "l2-confirmed", sessionKey, summary: p.summary, voice });
      return `（系统：用户已口头同意刚才待确认的操作「${p.summary}」。${retry}）`;
    }
    if (!p.rememberable) {
      store.audit({ kind: "tool", decision: "l2-remember-refused", sessionKey, summary: p.summary, category: p.category, voice });
      const what = p.category ? CATEGORY_CN[p.category] : "这类操作";
      return `（系统：用户同意了这一次「${p.summary}」，但「${what}」每次都要问，不能记住。${retry}做完后用一句话告诉用户：这类以后还会再问。）`;
    }
    const built = buildRememberedOverride(p.toolName, p.params, p.category, p.summary);
    const saved = built.ok ? appendOverride(store, built.override) : undefined;
    if (!built.ok || !saved?.ok) {
      const reason = !built.ok ? built.reason : (saved as { reason: string }).reason;
      store.audit({ kind: "tool", decision: "l2-remember-failed", sessionKey, summary: p.summary, reason, voice });
      const why = REMEMBER_FAIL_CN[reason] ?? "保存放行规则失败";
      return `（系统：用户同意了这一次「${p.summary}」，但${why}，下次还会问。${retry}做完后用一句话告诉用户这一点。）`;
    }
    store.audit({ kind: "tool", decision: "l2-remembered", sessionKey, summary: p.summary, overrideId: saved.id, existed: saved.existed, voice });
    return `（系统：用户已口头同意「${p.summary}」并要求以后不用再问，已记住（可在管家面板撤销）。${retry}做完后用一句话告诉用户：以后这件事直接做。）`;
  }

  /** 微信主人确认只由可信 requester 直接写入闩锁，不经过模型猜测。 */
  function onOwnerWechatTurn(sessionKey: string | undefined, userText: string, requester: any): string | undefined {
    if (sessionKey !== "agent:main:main" || requester?.channel !== "openclaw-weixin" || requester?.senderIsOwner !== true) return undefined;
    const decision = isConfirmation(userText) ? "approve" : isRejection(userText) ? "reject" : undefined;
    if (!decision) return undefined;
    const pendingRecord = approvals.latest(sessionKey);
    if (!pendingRecord || !approvals.decide(pendingRecord.id, decision, sessionKey)) return undefined;
    store.audit({ kind: "pending", decision: `wechat-${decision}`, sessionKey, pendingId: pendingRecord.id, summary: pendingRecord.summary });
    return decision === "approve" ? approvals.instruction(sessionKey) : "（系统：主人已拒绝刚才待确认操作，不要重试。）";
  }

  /** main binding 会抹掉 senderId 时，只信任同 run 的一次性签名 provenance。 */
  function onProvenanceWechatTurn(ctx: any, userText: string): string | undefined {
    const decision = isConfirmation(userText) ? "approve" : isRejection(userText) ? "reject" : undefined;
    if (!decision || !consumeOwnerWechatProvenance(store, ctx)) return undefined;
    const pendingRecord = approvals.latest(ctx.sessionKey);
    if (!pendingRecord || !approvals.decide(pendingRecord.id, decision, ctx.sessionKey)) return undefined;
    store.audit({ kind: "pending", decision: `wechat-${decision}`, sessionKey: ctx.sessionKey, pendingId: pendingRecord.id, summary: pendingRecord.summary });
    return decision === "approve" ? approvals.instruction(ctx.sessionKey) : "（系统：主人已拒绝刚才待确认操作，不要重试。）";
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

    // exec 的 ask 参数：模型自己要求审批（always / on-miss）会触发 OpenClaw 自带的 exec 审批，没有聊天路由、主人也答不了，
    // 只会表现为"等审批超时"。我们的"需要确认"一律走工具返回 + 口头确认，所以放行时把它改成不加严的 off。
    // 宿主把钩子返回的 params 浅合并到原参数上（{ ...原参数, ...返回值 }），不能靠漏掉键来删，只能改值。
    // 只在真正执行的那次（exec 本身，含 tool_call 的内层）改；外层 tool_call 的参数结构不同，不动。
    const askOriginal = rawToolName === "exec" && rawParams.ask != null && rawParams.ask !== "off" ? String(rawParams.ask) : undefined;
    const askRewrite = askOriginal === undefined ? undefined : { params: { ask: "off" } };
    const askNote = askOriginal === undefined ? {} : { askStripped: askOriginal };

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
        ...askNote,
        decision: "allow-inner-dedup",
      });
      return askRewrite;
    }

    const levels = store.readJson<LevelsFile>("levels.json", defaults);
    const target = rawToolName === "tool_call" ? resolveToolCallTarget(rawParams) : undefined;
    const toolName = target?.toolName ?? rawToolName;
    const params = target?.params ?? rawParams;
    const sessionKey: string | undefined = ctx?.sessionKey;
    const source = runSources.get(ctx?.runId ?? event.runId) ?? String(ctx?.trigger ?? "user");
    const proactive = isProactive(ctx?.runId ?? event.runId, sessionKey);
    // client 工具没有内层钩子；裸名字可能按名字匹配到 client 工具；选择器矛盾时真实目标不确定——三种都拒绝
    const forceL3 = target?.source === "client" || target?.source === "ambiguous" || (!!target && target.source === undefined && !levels.tools[target.toolName]);
    const c: Classification = forceL3
      ? { level: "L3", source: "client", note: "tool_call 的真实目标无法确定（client 来源、选择器矛盾或不在分级表里的裸名字）", rememberable: false, downgraded: false }
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
      ...(c.category ? { category: c.category } : {}),
      ...(c.overrideId ? { overrideId: c.overrideId } : {}),
      ...(target ? { via: "tool_call", catalogSource: target.source ?? "bare" } : {}),
    };
    const emitPermissionShadow = () => {
      if (!options?.permissionShadowEnabled && !options?.observePermissionShadow) return;
      try {
        const observation = evaluateShadowPermission({
          toolName,
          level: c.level,
          source: c.source,
          proactive,
          hasSessionKey: Boolean(sessionKey),
          actionFingerprint: actionHash64Of(toolName, params),
        });
        if (options?.observePermissionShadow) {
          options.observePermissionShadow(observation);
        } else if (options?.permissionShadowEnabled) {
          store.audit({
            kind: "permission-shadow",
            tool: observation.toolName,
            level: observation.level,
            proactive: observation.proactive,
            decision: observation.decision,
            actionFingerprint: observation.actionFingerprint,
            reason: observation.reason,
          });
        }
      } catch {
        // Passive shadow observation must never alter Guard decisions or throw.
      }
    };
    emitPermissionShadow();
    /** 只有外层 tool_call 需要登记；直接调用的工具不会再触发内层 */
    const grantIfOuter = () => {
      if (target) grantOuter(callId, { toolName, fingerprint: fingerprintOf(toolName, params), level: c.level, sessionKey });
    };

    // O1-B：批准后的下一次调用只能消耗同会话、同来源、同工具、同规范参数的一次性闩锁。
    // 这里不调用工具；仍由 OpenClaw 的正常执行路径执行，故无法绕过原始工具权限模型。
    if (approvals.consume(sessionKey, source, toolName, params)) {
      grantIfOuter();
      store.audit({ ...base, decision: "allow-approved-latch" });
      return askRewrite;
    }

    if (c.level === "L0" || c.level === "L1") {
      grantIfOuter();
      store.audit({ ...base, ...askNote, decision: "allow" });
      return askRewrite;
    }

    if (c.level === "L3" && c.source === "client") {
      store.audit({ ...base, decision: "block-refused" });
      return {
        block: true,
        blockReason: `已拒绝：${summary}。原因：${c.note}。不要换写法重试；用工具目录里的完整 id 调用（例如 openclaw:core:exec），或直接调用该工具。`,
      };
    }

    // L2 口头确认；分级表里手写的 L3（v2 默认表没有）也按"先问、不可记住"处理，旧表配新代码时不会把以前要审批的事全拒掉
    const rememberable = c.level === "L2" && c.rememberable;
    const fp = fingerprintOf(toolName, params);
    const p = sessionKey ? pending.get(sessionKey) : undefined;
    if (p && p.approved && p.fingerprint === fp && p.expiresAt > Date.now()) {
      pending.delete(sessionKey!);
      grantIfOuter();
      store.audit({ ...base, ...askNote, decision: "allow-confirmed" });
      return askRewrite;
    }
    const what = c.category ? CATEGORY_CN[c.category] : "需要确认的操作";
    if (proactive || !sessionKey) {
      store.audit({ ...base, decision: "block-no-confirmer" });
      return {
        block: true,
        blockReason: `需要确认（${what}）：${summary}。这是定时任务或心跳里发起的，没人能当场确认，已拦下并记录。不要重试，也不要换写法；等主人在场时再提。`,
      };
    }
    pending.set(sessionKey, { sessionKey, fingerprint: fp, summary, toolName, params, category: c.category, rememberable, expiresAt: Date.now() + PENDING_TTL_MS, approved: false });
    const opened = approvals.open({ toolName, params, sessionKey, source, summary });
    onTaskApproval?.({ sessionKey, approvalId: opened.id, description: opened.summary, expiresAt: opened.expiresAt });
    store.audit({ ...base, decision: "block-need-confirm" });
    const how = rememberable
      ? "用户用自然短句同意即可，例如「行你关吧」「好去弄吧」「允许关机」；说「以后都允许」才会记住。不要要求用户必须单独说某两个字。"
      : "用户用自然短句同意即可，例如「行你关吧」「好去弄吧」「允许关机」；这类每次都要问。不要要求用户必须单独说某两个字。";
    return {
      block: true,
      blockReason: `需要确认（${what}）：${summary}。先用一句口语问用户要不要这么做，不要让用户去微信或网页里点。${how}用户同意后，用完全相同的参数再调用一次；不要换写法绕过。`,
    };
  }

  return { beforeToolCall, onUserTurn, onOwnerWechatTurn, onProvenanceWechatTurn, noteRun, isProactive, approvals };
}
