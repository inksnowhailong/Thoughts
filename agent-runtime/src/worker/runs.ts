/**
 * Worker 运行管理（Wave1-C）：运行注册表、SSE 事件流（?since=seq 续传）、
 * resume/cancel、checkpoint 持久化（SqliteSaver 单实例，thread_id=run_id）、
 * 事件审计转发（POST bridge /internal/audit）。
 *
 * - createRun(body)：校验 WorkerRunRequest（run_id 缺省时服务端补 uuid）、
 *   存注册表并立即后台执行图；
 * - resume(runId,{decision,credential})：Command(resume) 唤醒 interrupt；
 *   注册表没有该 run 时从 checkpoint 探测并 revive（进程重启恢复）；
 * - cancel(runId,{reason})：置取消标记（plan/act 节点自检，当前工具完成后终止）；
 *   interrupt 挂起中则用 resume({decision:'reject'}) 唤醒走拒绝收尾；
 * - 事件按序持久化在内存列表（SSE 断开后 ?since=seq 续传），
 *   同时 fire-and-forget POST 一份到 bridge /internal/audit。
 */
import { mkdirSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { HumanMessage, SystemMessage } from '@langchain/core/messages';
import type { BaseChatModel } from '@langchain/core/language_models/chat_models';
import { Command, isInterrupted } from '@langchain/langgraph';
import { SqliteSaver } from '@langchain/langgraph-checkpoint-sqlite';
import { taskSpecSchema, type TaskSpec } from '../contracts/task-spec.js';
import {
  resumeRequestSchema,
  workerRunEventSchema,
  workerRunRequestSchema,
  type ResumeRequest,
  type WorkerRunEvent,
  type WorkerRunRequest,
} from '../contracts/worker-api.js';
import { loadRuntimeConfig } from '../config.js';
import { compileRole } from '../prompts/compiler.js';
import { REPO_ROOT } from '../tools/paths.js';
import { toLangChainTools, type ToolDeps } from '../tools/registry.js';
import { getWorkerChatModel } from './model.js';
import { buildWorkerGraph, sanitizeIdentifier, type FinalOutcome, type PendingApproval, type WorkerGraphCtx } from './graph.js';

export type RunStatus = 'running' | 'awaiting_approval' | 'completed' | 'failed' | 'cancelled';

export interface StoredEvent {
  seq: number;
  event: WorkerRunEvent;
}

export interface WorkerRuntimeOptions {
  bridgeUrl?: string;
  token?: string;
  /** 测试注入假模型；缺省 getChatModel('worker')。 */
  model?: BaseChatModel;
  /** checkpoint SQLite 路径；缺省 data/agent-runtime/checkpoints.sqlite3。 */
  checkpointPath?: string;
  /** 审批决定回调地址（bridge 登记时填 callback）；缺省 http://127.0.0.1:{port}。 */
  callbackBase?: string;
  /** 是否把事件 POST 到 bridge /internal/audit；缺省 true。 */
  audit?: boolean;
  /** 测试注入 saver（注入者负责关闭）。 */
  saver?: SqliteSaver;
}

/** 带 HTTP 状态码的错误（routes 层映射响应码）。 */
export class HttpError extends Error {
  constructor(
    readonly statusCode: number,
    message: string,
  ) {
    super(message);
    this.name = 'HttpError';
  }
}

export function isTerminal(status: RunStatus): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled';
}

interface RunCtxInfo {
  spec: TaskSpec;
  allowed: string[];
  sessionId: string;
  taskSpecWire: Record<string, unknown>;
}

interface RunRecord {
  runId: string;
  status: RunStatus;
  events: StoredEvent[];
  cancelRequested: boolean;
  cancelReason: string;
  ctxInfo: RunCtxInfo;
}

// ---------- worker prompt 编译（compileRole('worker') 要求 GOAL/MATERIALS/ACCEPTANCE 全给） ----------

function buildWorkerPromptVars(spec: TaskSpec, request: WorkerRunRequest): {
  GOAL: string;
  MATERIALS: string;
  ACCEPTANCE: string;
} {
  const goalLines = [spec.goal];
  if (spec.constraints.length > 0) goalLines.push('约束：', ...spec.constraints.map((c) => `- ${c}`));
  const references = (request.materials.references ?? []).map((r) => `- ${r.uri}（${r.kind}）`);
  const memories = request.memory_packet.summaries.map((s) => `- ${s}`);
  const materialLines = [
    ...(references.length > 0 ? ['引用材料：', ...references] : []),
    ...(memories.length > 0 ? ['记忆摘要：', ...memories] : []),
  ];
  return {
    GOAL: goalLines.join('\n'),
    MATERIALS: materialLines.length > 0 ? materialLines.join('\n') : '无',
    ACCEPTANCE:
      spec.success_criteria.length > 0
        ? spec.success_criteria.map((c, i) => `${i + 1}. ${c}`).join('\n')
        : '按任务目标完成即可。',
  };
}

// ---------- 运行管理器 ----------

export class WorkerRunManager {
  private readonly bridgeUrl: string;
  private readonly token: string;
  private readonly checkpointPath: string;
  private readonly callbackBase: string;
  private readonly audit: boolean;
  private ownSaver: SqliteSaver | null = null;
  private readonly runs = new Map<string, RunRecord>();

  constructor(private readonly opts: WorkerRuntimeOptions = {}) {
    const cfg = loadRuntimeConfig();
    this.bridgeUrl = opts.bridgeUrl ?? cfg.bridgeUrl;
    this.token = opts.token ?? cfg.token;
    this.callbackBase = opts.callbackBase ?? `http://127.0.0.1:${cfg.port}`;
    this.audit = opts.audit !== false;
    this.checkpointPath = opts.checkpointPath ?? path.join(REPO_ROOT, 'data', 'agent-runtime', 'checkpoints.sqlite3');
  }

  // ---------- 生命周期 ----------

  createRun(body: unknown): { run_id: string } {
    const raw = { ...(body as Record<string, unknown>) };
    if (typeof raw.run_id !== 'string' || raw.run_id.length === 0) raw.run_id = randomUUID();
    const request = workerRunRequestSchema.parse(raw);
    const spec = taskSpecSchema.parse(request.task_spec);
    if (this.runs.has(request.run_id)) return { run_id: request.run_id }; // 幂等：重复创建不重复执行
    const sessionId = `task/${spec.task_id}/r${spec.revision}`;
    const rec: RunRecord = {
      runId: request.run_id,
      status: 'running',
      events: [],
      cancelRequested: false,
      cancelReason: '',
      ctxInfo: {
        spec,
        allowed: [...request.allowed_capabilities],
        sessionId,
        taskSpecWire: request.task_spec as Record<string, unknown>,
      },
    };
    this.runs.set(rec.runId, rec);
    void this.executeRun(rec, request);
    return { run_id: rec.runId };
  }

  async resume(runId: string, body: unknown): Promise<{ ok: true }> {
    const req = resumeRequestSchema.parse(body);
    const rec = this.runs.get(runId) ?? (await this.reviveRun(runId));
    if (!rec) throw new HttpError(404, `worker run ${runId} 不存在`);
    if (rec.status !== 'awaiting_approval') {
      throw new HttpError(409, `worker run ${runId} 状态为 ${rec.status}，不能 resume`);
    }
    rec.status = 'running';
    this.appendEvent(rec, {
      type: 'progress',
      percent: null,
      stage: 'resumed',
      message: `审批${req.decision === 'approve' ? '通过' : '被拒绝'}`,
    });
    void this.resumeGraph(rec, req);
    return { ok: true };
  }

  async cancel(runId: string, body: unknown): Promise<{ ok: true; status: RunStatus }> {
    const raw = (body ?? {}) as { reason?: unknown };
    const reason = typeof raw.reason === 'string' ? raw.reason : '';
    const rec = this.runs.get(runId) ?? (await this.reviveRun(runId));
    if (!rec) throw new HttpError(404, `worker run ${runId} 不存在`);
    if (isTerminal(rec.status)) return { ok: true, status: rec.status };
    rec.cancelRequested = true;
    rec.cancelReason = reason;
    if (rec.status === 'awaiting_approval') {
      // 图在 interrupt 挂起：用 reject 唤醒走拒绝收尾，plan 节点随后按取消标记收场。
      rec.status = 'running';
      this.appendEvent(rec, {
        type: 'progress',
        percent: null,
        stage: 'resumed',
        message: '取消：拒绝挂起的审批',
      });
      void this.resumeGraph(rec, { decision: 'reject', credential: null });
    }
    return { ok: true, status: rec.status };
  }

  getRecord(runId: string): RunRecord | null {
    return this.runs.get(runId) ?? null;
  }

  eventsSince(runId: string, since: number): StoredEvent[] {
    const rec = this.runs.get(runId);
    if (!rec) return [];
    return rec.events.filter((stored) => stored.seq > since);
  }

  recordStatus(runId: string): RunStatus | null {
    return this.runs.get(runId)?.status ?? null;
  }

  /** 关闭自建 saver（测试模拟进程重启用；注入的 saver 由注入者负责）。 */
  dispose(): void {
    if (this.ownSaver !== null) {
      try {
        this.ownSaver.db.close();
      } catch {
        // 连接可能已关闭
      }
      this.ownSaver = null;
    }
  }

  // ---------- 内部：图执行 ----------

  private getSaver(): SqliteSaver {
    if (this.opts.saver) return this.opts.saver;
    this.ownSaver ??= (() => {
      mkdirSync(path.dirname(this.checkpointPath), { recursive: true });
      return SqliteSaver.fromConnString(this.checkpointPath);
    })();
    return this.ownSaver;
  }

  private buildCtx(rec: RunRecord, emit: (event: WorkerRunEvent) => void): WorkerGraphCtx {
    const { spec, allowed, sessionId } = rec.ctxInfo;
    const deps: ToolDeps = { bridgeUrl: this.bridgeUrl, token: this.token, session_id: sessionId };
    const tools = toLangChainTools(allowed.length > 0 ? { ids: allowed } : { workerVisible: true }, deps);
    const model = this.opts.model ?? getWorkerChatModel();
    const registerApproval = async (approval: PendingApproval): Promise<void> => {
      try {
        await fetch(`${this.bridgeUrl}/internal/bridge/worker_approval`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${this.token}`,
          },
          body: JSON.stringify({
            task_id: spec.task_id,
            capability: approval.capabilityId,
            args_summary: [...approval.summary].slice(0, 120).join(''),
            todo_id: approval.todoId,
            callback: this.callbackBase,
            run_id: rec.runId,
          }),
        });
      } catch {
        // 登记失败不阻断挂起：resume 通道仍可用，只是面板 /latest 看不到
      }
    };
    return {
      spec,
      model,
      tools,
      deps,
      emit,
      isCancelled: () => rec.cancelRequested,
      cancelReason: () => rec.cancelReason,
      registerApproval,
    };
  }

  private async executeRun(rec: RunRecord, request: WorkerRunRequest): Promise<void> {
    const emit = (event: WorkerRunEvent) => this.appendEvent(rec, event);
    try {
      const ctx = this.buildCtx(rec, emit);
      const graph = buildWorkerGraph(ctx).compile({ checkpointer: this.getSaver() });
      const input = {
        messages: [
          new SystemMessage(compileRole('worker', buildWorkerPromptVars(rec.ctxInfo.spec, request)).system),
          new HumanMessage('请开始执行任务。'),
        ],
        taskSpecWire: rec.ctxInfo.taskSpecWire,
        allowedCapabilities: rec.ctxInfo.allowed,
        logicalSession: rec.ctxInfo.sessionId,
      };
      emit({ type: 'progress', percent: 0, stage: 'starting', message: null });
      const result = await graph.invoke(input, { configurable: { thread_id: rec.runId } });
      this.consumeGraphResult(rec, result);
    } catch (e) {
      const message = sanitizeIdentifier(`worker run 执行异常：${e instanceof Error ? e.message : String(e)}`);
      rec.status = 'failed';
      if (!this.hasOutcome(rec)) {
        emit({ type: 'outcome', status: 'failed', summary: message, result: null });
      }
    }
  }

  private async resumeGraph(rec: RunRecord, req: ResumeRequest): Promise<void> {
    const emit = (event: WorkerRunEvent) => this.appendEvent(rec, event);
    try {
      const ctx = this.buildCtx(rec, emit);
      const graph = buildWorkerGraph(ctx).compile({ checkpointer: this.getSaver() });
      const result = await graph.invoke(
        new Command({ resume: { decision: req.decision, credential: req.credential ?? null } }),
        { configurable: { thread_id: rec.runId } },
      );
      this.consumeGraphResult(rec, result);
    } catch (e) {
      const message = sanitizeIdentifier(`worker run 恢复异常：${e instanceof Error ? e.message : String(e)}`);
      rec.status = 'failed';
      if (!this.hasOutcome(rec)) {
        emit({ type: 'outcome', status: 'failed', summary: message, result: null });
      }
    }
  }

  /** 图执行结果 → 运行状态：interrupt → awaiting_approval；finalOutcome → 终态。 */
  private consumeGraphResult(rec: RunRecord, result: unknown): void {
    if (isInterrupted(result)) {
      rec.status = 'awaiting_approval';
      this.appendEvent(rec, {
        type: 'progress',
        percent: null,
        stage: 'awaiting_approval',
        message: '等待主人审批',
      });
      return;
    }
    const final = (result as { finalOutcome?: FinalOutcome | null }).finalOutcome;
    if (final && isTerminal(final.status)) {
      rec.status = final.status;
      return;
    }
    if (!this.hasOutcome(rec)) {
      rec.status = 'failed';
      this.appendEvent(rec, {
        type: 'outcome',
        status: 'failed',
        summary: 'worker run 未产生终态结果',
        result: null,
      });
    }
  }

  /** 注册表没有该 run 时从 checkpoint 探测并复活（进程重启恢复，事件从 seq=0 重新开始）。
   * 只有审批挂起的 run 可复活（act 节点已把 pendingApproval 提交进 checkpoint）；
   * 其余半路状态按计划语义属于 interrupted，不伪装可恢复。 */
  private async reviveRun(runId: string): Promise<RunRecord | null> {
    const saver = this.getSaver();
    const tuple = await saver.getTuple({ configurable: { thread_id: runId } } as never);
    if (!tuple) return null;
    const channelValues =
      (tuple.checkpoint as { channel_values?: Record<string, unknown> }).channel_values ?? {};
    if (channelValues.pendingApproval === null || typeof channelValues.pendingApproval !== 'object') {
      return null;
    }
    const specWire = channelValues.taskSpecWire;
    if (specWire === null || typeof specWire !== 'object') return null;
    const spec = taskSpecSchema.parse(specWire);
    const allowed = Array.isArray(channelValues.allowedCapabilities)
      ? (channelValues.allowedCapabilities as string[])
      : [];
    const sessionId =
      typeof channelValues.logicalSession === 'string' && channelValues.logicalSession
        ? channelValues.logicalSession
        : `task/${spec.task_id}/r${spec.revision}`;
    const rec: RunRecord = {
      runId,
      status: 'awaiting_approval',
      events: [],
      cancelRequested: false,
      cancelReason: '',
      ctxInfo: { spec, allowed, sessionId, taskSpecWire: specWire as Record<string, unknown> },
    };
    this.runs.set(runId, rec);
    return rec;
  }

  private hasOutcome(rec: RunRecord): boolean {
    return rec.events.some((stored) => stored.event.type === 'outcome');
  }

  private appendEvent(rec: RunRecord, event: WorkerRunEvent): void {
    const parsed = workerRunEventSchema.safeParse(event);
    if (!parsed.success) {
      // 契约偏差只降级不炸进程：worker 服务必须活着，SSE 仍能观察到发生了什么
      console.error(`[worker] 事件不符合契约（降级为 internal_error）：${JSON.stringify(event)}`);
      const fallback = workerRunEventSchema.parse({
        type: 'progress',
        percent: null,
        stage: 'internal_error',
        message: 'worker 事件未通过契约校验，已降级记录',
      });
      rec.events.push({ seq: rec.events.length, event: fallback });
      return;
    }
    const stored: StoredEvent = { seq: rec.events.length, event: parsed.data };
    rec.events.push(stored);
    if (this.audit && this.bridgeUrl) {
      void fetch(`${this.bridgeUrl}/internal/audit`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${this.token}`,
        },
        body: JSON.stringify({
          event: { kind: 'worker_run_event', run_id: rec.runId, seq: stored.seq, ...stored.event },
        }),
      }).catch(() => undefined);
    }
  }
}
