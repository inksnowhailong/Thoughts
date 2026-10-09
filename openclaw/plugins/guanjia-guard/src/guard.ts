import crypto from "node:crypto";
import { classify, type LevelsFile } from "./levels";
import type { Store } from "./store";

/**
 * 分级中间层（D6）：before_tool_call 里定级并放行 / 拦截 / 走审批。
 *
 *   L0/L1 ──> 放行
 *   L2    ──> 拦下，让模型先问一句；用户下一句说"确认"后，同参数重试放行一次
 *   L3    ──> requireApproval，走 approvals.plugin 推到微信 + 桌面 Control UI
 *   cron/心跳会话：先降一档再按上面处理；L2 在主动会话里无人可答，直接拦
 */

/** 等待口头确认的 L2 动作（进程内存即可，3 分钟过期，网关重启丢了也只是要重问） */
interface PendingAction {
  sessionKey: string;
  fingerprint: string;
  summary: string;
  expiresAt: number;
  approved: boolean;
}

const PENDING_TTL_MS = 3 * 60_000;
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

export function createGuard(store: Store, defaults: LevelsFile) {
  const pending = new Map<string, PendingAction>();
  /** runId → 触发来源（user/cron/heartbeat），由 prompt 阶段钩子登记；工具钩子上下文里没有 trigger */
  const runTriggers = new Map<string, string>();

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
    if (!CONFIRM_RE.test(userText.trim())) {
      pending.delete(sessionKey);
      store.audit({ kind: "tool", decision: "l2-dropped", sessionKey, summary: p.summary });
      return undefined;
    }
    p.approved = true;
    p.expiresAt = Date.now() + PENDING_TTL_MS;
    store.audit({ kind: "tool", decision: "l2-confirmed", sessionKey, summary: p.summary });
    return `（系统：用户已口头确认刚才待确认的操作「${p.summary}」。现在用与刚才完全相同的参数再调用一次该工具。）`;
  }

  /** before_tool_call 处理器 */
  function beforeToolCall(event: any, ctx: any) {
    const levels = store.readJson<LevelsFile>("levels.json", defaults);
    const params = (event.params ?? {}) as Record<string, unknown>;
    const toolName: string = event.toolName;
    const sessionKey: string | undefined = ctx?.sessionKey;
    const proactive = isProactive(ctx?.runId ?? event.runId, sessionKey);
    const c = classify(levels, { toolName, params, derivedPaths: event.derivedPaths, sessionKey }, proactive);
    const summary = summarize(toolName, params);
    const base = { kind: "tool", tool: toolName, level: c.level, source: c.source, proactive, sessionKey, summary, paramKeys: Object.keys(params).sort() };

    if (c.level === "L0" || c.level === "L1") {
      store.audit({ ...base, decision: "allow" });
      return;
    }

    if (c.level === "L2") {
      const fp = fingerprintOf(toolName, params);
      const p = sessionKey ? pending.get(sessionKey) : undefined;
      if (p && p.approved && p.fingerprint === fp && p.expiresAt > Date.now()) {
        pending.delete(sessionKey!);
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
        timeoutMs: 300_000,
        onResolution: (decision: string) => store.audit({ ...base, decision: `approval-${decision}` }),
      },
    };
  }

  return { beforeToolCall, onUserTurn, noteRun, isProactive };
}
