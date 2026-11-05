/**
 * Tool Registry 门面（Wave1-B）：能力声明 → LangChain 工具 + Policy 门禁 + 审计。
 *
 * 工具执行流程（每次调用都过，无绕过路径）：
 *   ① Zod 校验参数；
 *   ② policyClient.check({session_id, capability_id, args})：
 *      allow → 执行 executor；deny → ToolResult ok:false error.code='policy_denied'；
 *      confirmation_required → 抛 PolicyConfirmationError（Main/Worker 捕获后走审批）；
 *   ③ executor 三类：local-process（child_process，无 shell）、http-python（bridge exec）、
 *      builtin-vlm（截屏 + 视觉模型）；
 *   ④ 每次执行 POST {bridgeUrl}/internal/audit 记审计（失败不阻断工具结果）。
 *
 * credential 重试：deps.credential 非空时，作为 x-guanjia-credential 请求头
 * 发给 bridge 的 exec/policy 接口校验一次性凭证（deps 是可变对象引用，
 * 调用方在审批后把 credential 写回同一对象即可生效）。
 */
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { tool } from '@langchain/core/tools';
import { CAPABILITIES } from './capabilities.js';
import { PolicyClient, type PolicyCheckResponse } from './policy-client.js';
import { getScreenVlmChatModel } from './vlm-model.js';
import type { Capability, LocalProcessExecutor, BuiltinVlmExecutor } from './types.js';
import { toolResultToDict, type ToolResult } from '../contracts/tool-result.js';
import type { JsonValue } from '../contracts/ids.js';

export interface ToolFilter {
  mainVisible?: boolean;
  workerVisible?: boolean;
  /** 显式按 id 选择（提供时优先于可见性过滤）。 */
  ids?: string[];
}

export interface ToolDeps {
  bridgeUrl: string;
  token: string;
  /** 会话标识；缺省 'voice-main'（工具执行上下文拿不到会话时）。 */
  session_id?: string;
  /** 审批后的一次性凭证；执行时写入即可让下一次调用带上。 */
  credential?: string;
}

/** 权限要求人工确认时抛出；Main/Worker 捕获后转为口头/面板审批流。 */
export class PolicyConfirmationError extends Error {
  constructor(
    readonly capabilityId: string,
    readonly todoId: string,
    readonly summary: string,
  ) {
    super(`capability "${capabilityId}" requires confirmation (todo ${todoId}): ${summary}`);
    this.name = 'PolicyConfirmationError';
  }
}

export function isPolicyConfirmationError(e: unknown): e is PolicyConfirmationError {
  return e instanceof PolicyConfirmationError;
}

/** 按过滤条件列出能力；ids 提供时优先于可见性过滤。 */
export function listCapabilities(filter?: ToolFilter): Capability[] {
  if (!filter) return [...CAPABILITIES];
  if (filter.ids !== undefined) {
    const wanted = new Set(filter.ids);
    return CAPABILITIES.filter((capability) => wanted.has(capability.id));
  }
  return CAPABILITIES.filter((capability) => {
    if (filter.mainVisible !== undefined && capability.mainVisible !== filter.mainVisible) return false;
    if (filter.workerVisible !== undefined && capability.workerVisible !== filter.workerVisible) return false;
    return true;
  });
}

// ---------- ToolResult 组装 ----------

function resultJson(result: ToolResult): string {
  return JSON.stringify(toolResultToDict(result));
}

function okResult(capabilityId: string, output: unknown): string {
  return resultJson({
    capability_id: capabilityId,
    ok: true,
    output: (output ?? null) as JsonValue,
    error: null,
    artifacts: [],
  });
}

function errorResult(capabilityId: string, code: string, message: string, retryable: boolean): string {
  return resultJson({
    capability_id: capabilityId,
    ok: false,
    output: null,
    error: { code, message, retryable },
    artifacts: [],
  });
}

// ---------- 审计 ----------

async function postAudit(deps: ToolDeps, event: Record<string, unknown>): Promise<void> {
  try {
    await fetch(`${deps.bridgeUrl}/internal/audit`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${deps.token}`,
      },
      body: JSON.stringify({ event }),
    });
  } catch {
    // 审计失败不阻断工具结果
  }
}

// ---------- 执行器 ----------

/** command 数组里的 {{key}} 占位符按校验后的参数替换；缺省参数替换为空串。 */
function substitute(command: string[], args: Record<string, unknown>): string[] {
  return command.map((part) =>
    part.replace(/\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}/g, (_whole, key: string) => {
      const value = args[key];
      return value === undefined || value === null ? '' : String(value);
    }),
  );
}

/** stdout 最后一行可解析的 JSON（脚本用退出码区分错误，非 0 也照样读）。 */
function lastJsonLine(stdout: string): Record<string, unknown> | undefined {
  const lines = stdout.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    try {
      const parsed = JSON.parse(lines[i]);
      if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // 继续往前找
    }
  }
  return undefined;
}

async function runProcess(
  command: string[],
  timeoutMs: number,
  cwd?: string,
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const child = spawn(command[0], command.slice(1), {
      windowsHide: true,
      cwd,
      timeout: timeoutMs,
    });
    let stdout = '';
    let stderr = '';
    child.stdout?.on('data', (chunk) => {
      stdout += String(chunk);
    });
    child.stderr?.on('data', (chunk) => {
      stderr += String(chunk);
    });
    child.on('error', (err) => reject(err));
    child.on('close', () => {
      const parsed = lastJsonLine(stdout);
      if (parsed !== undefined) resolve(parsed);
      else reject(new Error(`no JSON on stdout (stderr: ${stderr.trim().slice(0, 300) || '空'})`));
    });
  });
}

function toolFailedMessage(raw: Record<string, unknown>): string {
  return JSON.stringify(raw).slice(0, 800);
}

async function executeLocalProcess(
  capability: Capability,
  executor: LocalProcessExecutor,
  args: Record<string, unknown>,
): Promise<string> {
  const raw = await runProcess(substitute(executor.command, args), executor.timeoutMs ?? 20_000, executor.cwd);
  if (raw.ok === true) {
    return okResult(capability.id, raw);
  }
  return errorResult(
    capability.id,
    typeof raw.error === 'string' && raw.error ? raw.error : 'tool_failed',
    toolFailedMessage(raw),
    false,
  );
}

async function executeHttpPython(
  capability: Capability,
  deps: ToolDeps,
  args: Record<string, unknown>,
  credential?: string,
): Promise<string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${deps.token}`,
  };
  if (credential) headers['x-guanjia-credential'] = credential;
  const response = await fetch(`${deps.bridgeUrl}/internal/tools/${capability.id}/exec`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ args, session_id: deps.session_id ?? 'voice-main', credential: credential ?? undefined }),
  });
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    // 非 JSON 按状态码处理
  }
  if (response.status !== 200) {
    return errorResult(
      capability.id,
      `bridge_exec_http_${response.status}`,
      `bridge exec ${capability.id} http_${response.status}: ${JSON.stringify(body).slice(0, 300)}`,
      response.status >= 500,
    );
  }
  if (body.ok === true) {
    return okResult(capability.id, body.output ?? null);
  }
  const error = (body.error ?? {}) as Record<string, unknown>;
  return errorResult(
    capability.id,
    typeof error.code === 'string' && error.code ? error.code : 'tool_failed',
    typeof error.message === 'string' && error.message ? error.message : JSON.stringify(body).slice(0, 800),
    false,
  );
}

async function executeBuiltinVlm(
  capability: Capability,
  executor: BuiltinVlmExecutor,
  args: Record<string, unknown>,
): Promise<string> {
  let imagePath: string | null = null;
  if (typeof args.capture_path === 'string' && args.capture_path.trim()) {
    imagePath = args.capture_path;
  } else {
    const raw = await runProcess(substitute(executor.captureCommand, args), executor.timeoutMs ?? 15_000);
    if (raw.ok !== true || typeof raw.path !== 'string') {
      return errorResult(capability.id, 'capture_failed', toolFailedMessage(raw), true);
    }
    imagePath = raw.path;
  }
  let data: Buffer;
  try {
    data = await readFile(imagePath);
  } catch (err) {
    return errorResult(capability.id, 'capture_failed', `读不到截图 ${imagePath}：${err instanceof Error ? err.message : String(err)}`, false);
  }
  const dataUrl = `data:image/jpeg;base64,${data.toString('base64')}`;
  const question =
    typeof args.question === 'string' && args.question.trim() ? args.question.trim() : '描述一下屏幕上的内容';
  const model = await getScreenVlmChatModel();
  const response = await model.invoke([
    {
      role: 'user',
      content: [
        { type: 'text', text: question },
        { type: 'image_url', image_url: { url: dataUrl } },
      ],
    },
  ]);
  const content = response.content;
  const description = typeof content === 'string' ? content : JSON.stringify(content);
  return okResult(capability.id, { description, capture_path: imagePath, question });
}

// ---------- 工具生成 ----------

/**
 * 为单个能力生成 LangChain 工具（toLangChainTools 的组成单元，测试可直接用）。
 * 工具名 = capability.id，description = capability.description，schema = inputSchema。
 */
export function buildLangChainTool(capability: Capability, deps: ToolDeps): Record<string, unknown> {
  const policyClient = new PolicyClient(deps.bridgeUrl, deps.token);
  const sessionId = () => deps.session_id ?? 'voice-main';
  const credential = () => (deps.credential && deps.credential.trim() ? deps.credential : undefined);
  const lcTool = tool(
    async (args: Record<string, unknown>) => {
      // ① Zod 校验
      const parsed = capability.inputSchema.safeParse(args ?? {});
      if (!parsed.success) {
        const issue = parsed.error.issues[0];
        await postAudit(deps, {
          kind: 'tool_execution',
          session_id: sessionId(),
          capability_id: capability.id,
          outcome: 'invalid_args',
          ok: false,
          error_code: 'invalid_args',
          detail: issue ? `${issue.path.join('.')}: ${issue.message}` : '',
        });
        return errorResult(
          capability.id,
          'invalid_args',
          issue ? `${issue.path.join('.') || 'args'}: ${issue.message}` : '参数校验失败',
          false,
        );
      }
      const validated = parsed.data as Record<string, unknown>;

      // ② Policy 门禁
      let decision: PolicyCheckResponse;
      try {
        decision = await policyClient.check({
          session_id: sessionId(),
          capability_id: capability.id,
          args: validated,
          credential: credential(),
        });
      } catch (err) {
        await postAudit(deps, {
          kind: 'tool_execution', session_id: sessionId(), capability_id: capability.id,
          outcome: 'error', ok: false, error_code: 'policy_client_error',
        });
        return errorResult(capability.id, 'policy_client_error', String(err), true);
      }
      if (decision.outcome === 'deny') {
        await postAudit(deps, {
          kind: 'tool_execution', session_id: sessionId(), capability_id: capability.id,
          outcome: 'policy_denied', ok: false, error_code: 'policy_denied', reason: decision.reason,
        });
        return errorResult(capability.id, 'policy_denied', decision.reason, false);
      }
      if (decision.outcome === 'confirmation_required') {
        if (!decision.todo_id) {
          return errorResult(capability.id, 'policy_client_error', 'bridge 返回 confirmation_required 但缺 todo_id', false);
        }
        await postAudit(deps, {
          kind: 'tool_execution', session_id: sessionId(), capability_id: capability.id,
          outcome: 'confirmation_required', ok: false, error_code: 'confirmation_required', todo_id: decision.todo_id,
        });
        throw new PolicyConfirmationError(capability.id, decision.todo_id, decision.summary ?? decision.reason);
      }

      // ③ 执行
      let result: string;
      try {
        if (capability.executor.kind === 'local-process') {
          result = await executeLocalProcess(capability, capability.executor, validated);
        } else if (capability.executor.kind === 'http-python') {
          result = await executeHttpPython(capability, deps, validated, credential());
        } else {
          result = await executeBuiltinVlm(capability, capability.executor, validated);
        }
      } catch (err) {
        if (isPolicyConfirmationError(err)) throw err;
        await postAudit(deps, {
          kind: 'tool_execution', session_id: sessionId(), capability_id: capability.id,
          outcome: 'error', ok: false, error_code: 'executor_failed',
        });
        return errorResult(capability.id, 'executor_failed', String(err), true);
      }
      // ④ 审计
      await postAudit(deps, {
        kind: 'tool_execution',
        session_id: sessionId(),
        capability_id: capability.id,
        outcome: 'executed',
        ok: true,
        credential_used: credential() !== undefined,
      });
      return result;
    },
    {
      name: capability.id,
      description: capability.description,
      schema: capability.inputSchema,
    },
  );
  // LangChain 工具类型带私有成员、无索引签名；契约要求返回 Record<string, unknown>，按已知形状桥接。
  return lcTool as unknown as Record<string, unknown>;
}

/** 按过滤条件与依赖生成 LangChain 工具数组（Main/Worker 绑定工具的唯一入口）。 */
export function toLangChainTools(filter: ToolFilter, deps: ToolDeps): Array<Record<string, unknown>> {
  return listCapabilities(filter).map((capability) => buildLangChainTool(capability, deps));
}
