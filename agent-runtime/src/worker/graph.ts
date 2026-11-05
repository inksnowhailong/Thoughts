/**
 * 通用 Worker Graph（LangGraph StateGraph）—— Wave1-C。
 *
 * 图（重构计划 §4.2：plan → act → verify → outcome，审批走 interrupt）：
 *   START → plan →（有 tool_calls）act →（循环上限/审批/继续）→ verify → outcome → END
 *   approval 节点开头 interrupt()：LangGraph JS 语义（spike 已验证）——interrupt 所在
 *   节点的状态更新【不】提交，恢复时节点重跑、interrupt() 返回 resume 值。因此：
 *   - 审批登记（POST bridge /internal/bridge/worker_approval）在 act/retry 节点内完成
 *     并随节点正常返回提交，挂起/恢复路径上不存在未提交的副作用；
 *   - 恢复时 approval 节点重跑，interrupt() 返回 {decision, credential}，
 *     approve → retry 节点带 deps.credential 重试同一工具；reject → 把拒绝作为
 *     ToolMessage 回给模型改道收尾。
 *
 * 其他要点：
 * - 工具循环上限 MAX_TOOL_STEPS=12：超限注入 SystemMessage 收尾提示并 forceFinalize
 *   （plan 不再绑定工具，模型只能给最终回复）；
 * - verify：本地检查最终回复非空；success_criteria 存在或回复为空时注入提示重试一次，
 *   之后放行（空回复由 outcome 判 failed）；
 * - cancel：ctx.isCancelled() 在 plan/act/retry 检查，当前工具完成后终止，
 *   图以 cancelled outcome 收尾（interrupt 挂起时由 runs.ts 用 resume(reject) 唤醒）；
 * - TaskResult 聚合：key_steps=工具调用轨迹、artifacts=工具产物路径（output.path /
 *   output.capture_path）、evidence.kind='agent_runtime_worker'、summary=模型最终回复。
 */
import { createHash, randomUUID } from 'node:crypto';
import { AIMessage, SystemMessage, ToolMessage, type BaseMessage } from '@langchain/core/messages';
import type { BaseChatModel } from '@langchain/core/language_models/chat_models';
import { Annotation, END, MessagesAnnotation, START, StateGraph, interrupt } from '@langchain/langgraph';
import { formatUtc, type JsonValue } from '../contracts/ids.js';
import type { TaskResultWire } from '../contracts/task-result.js';
import { artifactRefToDict, type ArtifactRef, type TaskSpec } from '../contracts/task-spec.js';
import type { WorkerRunEvent } from '../contracts/worker-api.js';
import { isPolicyConfirmationError, type ToolDeps } from '../tools/registry.js';

/** 工具循环上限（防失控）。 */
export const MAX_TOOL_STEPS = 12;

/** 挂起审批的完整上下文（state.pendingApproval）。 */
export interface PendingApproval {
  capabilityId: string;
  todoId: string;
  summary: string;
  args: Record<string, unknown>;
  toolName: string;
  toolCallId: string;
}

/** 图终态（outcome 节点写入 state.finalOutcome）。 */
export interface FinalOutcome {
  status: 'completed' | 'failed' | 'cancelled';
  summary: string;
  result: TaskResultWire | null;
}

/** 建图上下文：每个 run 一份（spec/工具子集/依赖/事件发射均随 run 定制）。 */
export interface WorkerGraphCtx {
  spec: TaskSpec;
  model: BaseChatModel;
  /** toLangChainTools 产物（Record 形状，见 registry 契约注释）。 */
  tools: Array<Record<string, unknown>>;
  deps: ToolDeps;
  emit: (event: WorkerRunEvent) => void;
  isCancelled: () => boolean;
  cancelReason: () => string;
  /** act/retry 捕获 PolicyConfirmationError 后先向 bridge 登记审批（失败不阻断挂起）。 */
  registerApproval: (approval: PendingApproval) => Promise<void>;
}

// ---------- State ----------

const WorkerState = Annotation.Root({
  ...MessagesAnnotation.spec,
  /** TaskSpec 线形状：checkpoint 恢复（进程重启后 revive）时重建建图上下文用。 */
  taskSpecWire: Annotation<Record<string, unknown>>({ reducer: (_a, b) => b, default: () => ({}) }),
  allowedCapabilities: Annotation<string[]>({ reducer: (_a, b) => b, default: () => [] }),
  logicalSession: Annotation<string>({ reducer: (_a, b) => b, default: () => '' }),
  pendingApproval: Annotation<PendingApproval | null>({ reducer: (_a, b) => b, default: () => null }),
  approvalDecision: Annotation<'approve' | 'reject' | null>({ reducer: (_a, b) => b, default: () => null }),
  approvalCredential: Annotation<string | null>({ reducer: (_a, b) => b, default: () => null }),
  progress: Annotation<number>({ reducer: (_a, b) => b, default: () => 0 }),
  /** 增量累加：act/retry 返回本节点执行的调用数（interrupt 不在 act/retry 内，无重复累加风险）。 */
  toolCallsCount: Annotation<number>({ reducer: (a, b) => a + b, default: () => 0 }),
  keySteps: Annotation<string[]>({ reducer: (a, b) => [...a, ...b], default: () => [] }),
  artifacts: Annotation<ArtifactRef[]>({ reducer: (a, b) => [...a, ...b], default: () => [] }),
  forceFinalize: Annotation<boolean>({ reducer: (_a, b) => b, default: () => false }),
  wasCancelled: Annotation<boolean>({ reducer: (_a, b) => b, default: () => false }),
  verifyRetries: Annotation<number>({ reducer: (_a, b) => b, default: () => 0 }),
  finalOutcome: Annotation<FinalOutcome | null>({ reducer: (_a, b) => b, default: () => null }),
});

export type WorkerStateType = typeof WorkerState.State;

// ---------- 工具结果处理 ----------

function parseToolJson(content: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(content);
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** identifier 契约是 1..200 码点、无控制字符：按码点截断并加省略号。 */
export function clampIdentifier(text: string, max = 200): string {
  const parts = [...text.trim()];
  if (parts.length === 0) return '(空)';
  return parts.length <= max ? parts.join('') : [...parts.slice(0, max - 1), '…'].join('');
}

/** 模型/工具文本进 identifier 字段前必须净化：控制字符（含换行）替换为空格再截断。 */
export function sanitizeIdentifier(text: string, max = 200): string {
  const cleaned = [...text]
    .map((c) => {
      const code = c.codePointAt(0) ?? 0;
      return code < 32 || code === 127 ? ' ' : c;
    })
    .join('');
  return clampIdentifier(cleaned, max);
}

/** 与 Python bridge.policy.canonical_args / action_fingerprint 逐字节一致的参数指纹。 */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(record[k])}`).join(',')}}`;
  }
  return JSON.stringify(value ?? null);
}

export function actionFingerprint(args: unknown): string {
  return createHash('sha256').update(canonicalJson(args ?? {}), 'utf8').digest('hex');
}

function traceSummary(result: string): string {
  const parsed = parseToolJson(result);
  if (parsed === null) return sanitizeIdentifier(result, 180);
  if (parsed.ok === true) {
    const output = parsed.output;
    if (output !== null && typeof output === 'object') {
      return sanitizeIdentifier(JSON.stringify(output), 180);
    }
    return sanitizeIdentifier(String(output ?? 'ok'), 180);
  }
  const error = (parsed.error ?? {}) as Record<string, unknown>;
  return sanitizeIdentifier(`${String(error.code ?? 'tool_failed')}: ${String(error.message ?? '')}`, 180);
}

/** 把一次工具结果聚进 key_steps/artifacts（增量 reducer，节点返回新增量）。 */
function collectFromToolResult(
  parsed: Record<string, unknown> | null,
  keySteps: string[],
  artifacts: ArtifactRef[],
): void {
  const capabilityId = typeof parsed?.capability_id === 'string' ? parsed.capability_id : 'tool';
  const ok = parsed?.ok === true;
  keySteps.push(`${capabilityId} → ${ok ? '成功' : '失败'}`);
  const output = parsed?.output;
  if (ok && output !== null && typeof output === 'object' && !Array.isArray(output)) {
    const record = output as Record<string, unknown>;
    const artifactPath =
      typeof record.path === 'string' && record.path.trim()
        ? record.path
        : typeof record.capture_path === 'string' && record.capture_path.trim()
          ? record.capture_path
          : null;
    if (artifactPath !== null) {
      artifacts.push({ uri: artifactPath, kind: capabilityId, sha256: null, media_type: null });
    }
  }
}

async function invokeTool(
  tools: Array<Record<string, unknown>>,
  name: string,
  args: Record<string, unknown>,
): Promise<string> {
  const tool = tools.find((t) => (t as { name?: unknown }).name === name);
  if (!tool) {
    return JSON.stringify({
      capability_id: name,
      ok: false,
      output: null,
      error: { code: 'tool_unavailable', message: `工具 ${name} 不在本任务允许的能力子集内`, retryable: false },
      artifacts: [],
    });
  }
  const out = await (tool as unknown as { invoke: (a: unknown) => Promise<unknown> }).invoke(args);
  return typeof out === 'string' ? out : JSON.stringify(out);
}

function lastAiText(messages: BaseMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.getType?.() === 'ai') {
      const content = (message as AIMessage).content;
      return typeof content === 'string' ? content : '';
    }
  }
  return '';
}

function hasPendingToolCalls(state: WorkerStateType): boolean {
  const last = state.messages[state.messages.length - 1];
  return last?.getType?.() === 'ai' && ((last as AIMessage).tool_calls?.length ?? 0) > 0;
}

// ---------- 节点 ----------

async function planNode(ctx: WorkerGraphCtx, state: WorkerStateType) {
  if (ctx.isCancelled()) return { wasCancelled: true, progress: state.progress };
  ctx.emit({
    type: 'progress',
    percent: Math.min(85, 10 + state.toolCallsCount * 8),
    stage: 'plan',
    message: null,
  });
  const bound =
    state.forceFinalize || ctx.tools.length === 0
      ? ctx.model
      : ctx.model.bindTools
        ? ctx.model.bindTools(ctx.tools as never)
        : ctx.model;
  const response = await bound.invoke(state.messages as never);
  const ai = response as AIMessage;
  return { messages: [ai] };
}

async function actNode(ctx: WorkerGraphCtx, state: WorkerStateType) {
  const last = state.messages[state.messages.length - 1] as AIMessage;
  const calls = last?.tool_calls ?? [];
  const newMessages: BaseMessage[] = [];
  const keySteps: string[] = [];
  const artifacts: ArtifactRef[] = [];
  let executed = 0;
  for (let i = 0; i < calls.length; i += 1) {
    const tc = calls[i];
    if (ctx.isCancelled()) {
      return { messages: newMessages, keySteps, artifacts, wasCancelled: true };
    }
    ctx.emit({
      type: 'progress',
      percent: Math.min(80, 20 + (state.toolCallsCount + executed) * 8),
      stage: 'act',
      message: `调用 ${tc.name}`,
    });
    const startedAt = Date.now();
    try {
      const result = await invokeTool(ctx.tools, tc.name, (tc.args ?? {}) as Record<string, unknown>);
      newMessages.push(new ToolMessage({ content: result, tool_call_id: tc.id ?? '', id: tc.id }));
      const parsed = parseToolJson(result);
      collectFromToolResult(parsed, keySteps, artifacts);
      executed += 1;
      ctx.emit({
        type: 'tool_trace',
        capability_id: tc.name,
        ok: parsed?.ok === true,
        duration_ms: Date.now() - startedAt,
        summary: traceSummary(result),
      });
    } catch (e) {
      if (isPolicyConfirmationError(e)) {
        const approval: PendingApproval = {
          capabilityId: e.capabilityId,
          todoId: e.todoId,
          summary: e.summary,
          args: (tc.args ?? {}) as Record<string, unknown>,
          toolName: tc.name,
          toolCallId: tc.id ?? '',
        };
        await ctx.registerApproval(approval);
        ctx.emit({
          type: 'approval_request',
          request_id: e.todoId,
          capability: e.capabilityId,
          rationale: sanitizeIdentifier(e.summary),
          action_fingerprint: actionFingerprint(tc.args),
        });
        // 挂起调用之后的剩余 tool_call 补占位响应，保证恢复后历史完整（每个 tool_call 都有 ToolMessage）。
        for (const skip of calls.slice(i + 1)) {
          newMessages.push(
            new ToolMessage({
              content: '此调用被跳过：前序工具触发了审批中断。若任务仍需要该操作，请重新发起。',
              tool_call_id: skip.id ?? '',
              id: skip.id,
            }),
          );
        }
        return {
          messages: newMessages,
          keySteps,
          artifacts,
          pendingApproval: approval,
          toolCallsCount: executed,
          progress: Math.min(80, 20 + (state.toolCallsCount + executed) * 8),
        };
      }
      const failure = `工具执行失败：${e instanceof Error ? e.message : String(e)}`;
      newMessages.push(new ToolMessage({ content: failure, tool_call_id: tc.id ?? '', id: tc.id }));
      executed += 1;
      ctx.emit({
        type: 'tool_trace',
        capability_id: tc.name,
        ok: false,
        duration_ms: Date.now() - startedAt,
        summary: sanitizeIdentifier(failure, 180),
      });
    }
  }
  const total = state.toolCallsCount + executed;
  const messages = [...newMessages];
  let forceFinalize = state.forceFinalize;
  if (total >= MAX_TOOL_STEPS && !forceFinalize) {
    forceFinalize = true;
    messages.push(
      new SystemMessage(
        `已达到工具调用上限（${MAX_TOOL_STEPS} 步）。请立即基于已有信息给出最终结论，不要再调用任何工具。`,
      ),
    );
  }
  return {
    messages,
    keySteps,
    artifacts,
    toolCallsCount: executed,
    forceFinalize,
    progress: Math.min(85, 20 + total * 8),
  };
}

function approvalNode(state: WorkerStateType) {
  const pa = state.pendingApproval!;
  const decision = interrupt({
    capabilityId: pa.capabilityId,
    todoId: pa.todoId,
    summary: pa.summary,
    args: pa.args,
  });
  const resolved = (decision ?? {}) as { decision?: unknown; credential?: unknown };
  if (resolved.decision === 'approve') {
    return {
      approvalDecision: 'approve' as const,
      approvalCredential:
        typeof resolved.credential === 'string' && resolved.credential.length > 0 ? resolved.credential : null,
    };
  }
  return { approvalDecision: 'reject' as const, approvalCredential: null };
}

async function retryNode(ctx: WorkerGraphCtx, state: WorkerStateType) {
  const pa = state.pendingApproval!;
  ctx.emit({
    type: 'progress',
    percent: state.progress,
    stage: 'act',
    message: `审批通过，重试 ${pa.toolName}`,
  });
  const startedAt = Date.now();
  if (state.approvalCredential) ctx.deps.credential = state.approvalCredential;
  let result: string;
  try {
    result = await invokeTool(ctx.tools, pa.toolName, pa.args);
  } catch (e) {
    ctx.deps.credential = undefined; // 一次性凭证用完即清，不泄漏到后续工具
    if (isPolicyConfirmationError(e)) {
      const approval: PendingApproval = {
        capabilityId: e.capabilityId,
        todoId: e.todoId,
        summary: e.summary,
        args: pa.args,
        toolName: pa.toolName,
        toolCallId: pa.toolCallId,
      };
      await ctx.registerApproval(approval);
      ctx.emit({
        type: 'approval_request',
        request_id: e.todoId,
        capability: e.capabilityId,
        rationale: sanitizeIdentifier(e.summary),
        action_fingerprint: actionFingerprint(pa.args),
      });
      return { pendingApproval: approval, approvalDecision: null, approvalCredential: null };
    }
    const failure = `工具执行失败：${e instanceof Error ? e.message : String(e)}`;
    return {
      messages: [new ToolMessage({ content: failure, tool_call_id: pa.toolCallId, id: pa.toolCallId })],
      pendingApproval: null,
      approvalDecision: null,
      approvalCredential: null,
      toolCallsCount: 1,
      keySteps: [`${pa.toolName} → 失败`],
    };
  }
  ctx.deps.credential = undefined;
  const parsed = parseToolJson(result);
  const ok = parsed?.ok === true;
  const keySteps: string[] = [];
  const artifacts: ArtifactRef[] = [];
  collectFromToolResult(parsed, keySteps, artifacts);
  ctx.emit({
    type: 'tool_trace',
    capability_id: pa.toolName,
    ok,
    duration_ms: Date.now() - startedAt,
    summary: traceSummary(result),
  });
  return {
    messages: [new ToolMessage({ content: result, tool_call_id: pa.toolCallId, id: pa.toolCallId })],
    pendingApproval: null,
    approvalDecision: null,
    approvalCredential: null,
    toolCallsCount: 1,
    keySteps,
    artifacts,
  };
}

function rejectNode(state: WorkerStateType) {
  const pa = state.pendingApproval!;
  return {
    messages: [
      new ToolMessage({
        content: `主人拒绝了「${pa.summary}」的审批，该操作未执行。请改用其他方式完成任务，或如实说明受阻原因。`,
        tool_call_id: pa.toolCallId,
        id: pa.toolCallId,
      }),
    ],
    pendingApproval: null,
    approvalDecision: null,
    approvalCredential: null,
  };
}

function verifyNode(ctx: WorkerGraphCtx, state: WorkerStateType) {
  const reply = lastAiText(state.messages);
  const empty = reply.trim().length === 0;
  if (state.verifyRetries >= 1) return {}; // 已重试过一次，放行（空回复由 outcome 判 failed）
  const criteria = ctx.spec.success_criteria;
  if (!empty && criteria.length === 0) return {};
  const lines =
    criteria.length > 0
      ? criteria.map((c, i) => `${i + 1}. ${c}`).join('\n')
      : '（无显式验收标准，按任务目标判断）';
  const hint = empty
    ? '你上一条回复为空。请根据任务目标与材料，给出明确、完整的最终结果报告（不要再调用工具）。'
    : `请对照以下验收标准逐条自查你的工作结果：若全部满足，请重申最终结论；若仍有未完成项，请继续完成后在回复中如实说明。\n${lines}`;
  ctx.emit({
    type: 'progress',
    percent: 85,
    stage: 'verify',
    message: empty ? '最终回复为空，要求重新作答' : '对照验收标准自评',
  });
  return { messages: [new SystemMessage(hint)], verifyRetries: 1 };
}

function outcomeNode(ctx: WorkerGraphCtx, state: WorkerStateType) {
  const reply = lastAiText(state.messages).trim();
  const cancelled = state.wasCancelled || ctx.isCancelled();
  const status: FinalOutcome['status'] = cancelled ? 'cancelled' : reply.length === 0 ? 'failed' : 'completed';
  const now = new Date();
  const summary =
    status === 'completed'
      ? sanitizeIdentifier(reply)
      : status === 'cancelled'
        ? sanitizeIdentifier(`任务已取消：${ctx.cancelReason() || reply || '按请求取消'}`)
        : '任务失败：模型未产生任何最终结果';
  const result: TaskResultWire | null =
    status === 'completed'
      ? {
          result_id: randomUUID(),
          task_id: ctx.spec.task_id,
          revision: ctx.spec.revision,
          summary: sanitizeIdentifier(reply),
          artifacts: state.artifacts.map(artifactRefToDict),
          evidence: [
            {
              kind: 'agent_runtime_worker',
              summary: sanitizeIdentifier(reply),
              observed_at: formatUtc(now),
              artifacts: state.artifacts.map(artifactRefToDict),
              details: {
                full_reply: [...reply].slice(0, 2000).join(''),
                key_steps: state.keySteps.slice(0, 50),
              } as Record<string, JsonValue>,
              evidence_id: null,
            },
          ],
          error: null,
          key_steps: state.keySteps.map((step) => sanitizeIdentifier(step)),
          evidence_at: formatUtc(now),
        }
      : null;
  ctx.emit({ type: 'progress', percent: 100, stage: 'outcome', message: null });
  ctx.emit({
    type: 'outcome',
    status,
    summary: status === 'completed' ? sanitizeIdentifier(reply) : summary,
    result,
  });
  return { finalOutcome: { status, summary, result } as FinalOutcome, progress: 100 };
}

// ---------- 图装配 ----------

function afterPlan(state: WorkerStateType): 'act' | 'verify' | 'outcome' {
  if (state.wasCancelled) return 'outcome';
  if (hasPendingToolCalls(state)) return state.forceFinalize ? 'verify' : 'act';
  return 'verify';
}

function afterAct(state: WorkerStateType): 'plan' | 'approval' | 'outcome' {
  if (state.wasCancelled) return 'outcome';
  if (state.pendingApproval !== null) return 'approval';
  return 'plan';
}

function afterApproval(state: WorkerStateType): 'retry' | 'reject' {
  return state.approvalDecision === 'approve' ? 'retry' : 'reject';
}

function afterRetry(state: WorkerStateType): 'plan' | 'approval' | 'outcome' {
  if (state.wasCancelled) return 'outcome';
  if (state.pendingApproval !== null) return 'approval';
  return 'plan';
}

function afterVerify(state: WorkerStateType): 'plan' | 'outcome' {
  const last = state.messages[state.messages.length - 1];
  // verify 注入了自评/补答 SystemMessage → 回 plan 一轮；放行则最后一条是模型回复。
  return last !== undefined && last.getType?.() === 'system' ? 'plan' : 'outcome';
}

export function buildWorkerGraph(ctx: WorkerGraphCtx) {
  return new StateGraph(WorkerState)
    .addNode('plan', (state: WorkerStateType) => planNode(ctx, state))
    .addNode('act', (state: WorkerStateType) => actNode(ctx, state))
    .addNode('approval', (state: WorkerStateType) => approvalNode(state))
    .addNode('retry', (state: WorkerStateType) => retryNode(ctx, state))
    .addNode('reject', (state: WorkerStateType) => rejectNode(state))
    .addNode('verify', (state: WorkerStateType) => verifyNode(ctx, state))
    .addNode('outcome', (state: WorkerStateType) => outcomeNode(ctx, state))
    .addEdge(START, 'plan')
    .addConditionalEdges('plan', afterPlan)
    .addConditionalEdges('act', afterAct)
    .addConditionalEdges('approval', afterApproval)
    .addConditionalEdges('retry', afterRetry)
    .addEdge('reject', 'plan')
    .addConditionalEdges('verify', afterVerify)
    .addEdge('outcome', END);
}

