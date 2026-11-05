/**
 * Main Agent：语音轮处理。
 *
 * - system prompt 由 Prompt Compiler 按 main 角色编译（分节+预算守门）；
 * - 模型流式输出文本增量（yield {type:'delta'}），结束带 usage（yield {type:'usage'}）；
 * - 工具循环：遇到 tool_call → 执行 → ToolMessage 回灌模型继续，最多 MAX_TOOL_ROUNDS 轮；
 * - 确认流：工具抛 PolicyConfirmationError（形状见 tool-mock/registry 契约）时，
 *   把挂起请求记入会话 pendingApproval，本轮由模型自然生成确认话术；
 *   下一轮用户消息先查 pendingApproval：肯定确认词 → 调 bridge /internal/policy/confirm
 *   换 credential，用相同参数自动重试工具；否定词 → 丢弃并简短回应；
 * - 会话历史含工具调用消息（AIMessage with tool_calls / ToolMessage），由 ConversationStore 持久化。
 */
import {
  AIMessage,
  AIMessageChunk,
  HumanMessage,
  SystemMessage,
  ToolMessage,
  type BaseMessage,
} from '@langchain/core/messages';
import type { BaseChatModel } from '@langchain/core/language_models/chat_models';
import { loadRuntimeConfig, type RuntimeConfig } from '../config.js';
import { getChatModel } from '../models/provider.js';
import { compileRole } from '../prompts/compiler.js';
import { ConversationStore, type PendingApproval } from './conversation.js';
// Wave1-C 接线：真实 Tool Registry（能力声明→schema→bridge 执行，先过 Python Policy）。
// 测试用 opts.toolsFactory 注入假工具（tool-mock），生产路径走 registry。
import { toLangChainTools } from '../tools/registry.js';

export type VoiceChunk =
  | { type: 'delta'; text: string }
  | { type: 'usage'; usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number } };

export interface VoiceTurnOptions {
  /** 测试注入假模型；缺省用 getChatModel('main')。 */
  model?: BaseChatModel;
  store?: ConversationStore;
  bridgeUrl?: string;
  token?: string;
  /** 测试注入假工具（tool-mock）；缺省真实 registry（mainVisible 能力子集）。 */
  toolsFactory?: (deps: { bridgeUrl: string; token: string }) => RuntimeTool[];
}

/** 工具最多执行轮数（防死循环）。 */
const MAX_TOOL_ROUNDS = 3;

// 确认词表（简单词表；语音确认一般 1-4 字，超过 6 字的当普通消息，先判否定再判肯定）
const NEGATIVE_WORDS = ['不用', '算了', '不要', '不行', '不好', '取消', '先不', '别'];
const AFFIRMATIVE_WORDS = ['允许', '可以', '确认', '同意', '好的', '好', '行', '嗯', '是', '要'];

function matchIntent(text: string): 'affirm' | 'deny' | null {
  const t = text.replace(/[\s。.!,、??:;~]+/g, '');
  if (t.length === 0 || t.length > 6) return null;
  if (NEGATIVE_WORDS.some((w) => t.includes(w))) return 'deny';
  if (AFFIRMATIVE_WORDS.some((w) => t.includes(w))) return 'affirm';
  return null;
}

/** PolicyConfirmationError 的结构判断（与 registry 契约的字段一致，不依赖 import，便于切换真实实现）。 */
function isPolicyConfirmationError(
  e: unknown,
): e is Error & { capabilityId: string; todoId: string; summary: string } {
  return (
    e instanceof Error &&
    typeof (e as { capabilityId?: unknown }).capabilityId === 'string' &&
    typeof (e as { todoId?: unknown }).todoId === 'string' &&
    typeof (e as { summary?: unknown }).summary === 'string'
  );
}

type RuntimeTool = ReturnType<typeof toLangChainTools>[number];

async function invokeTool(tools: RuntimeTool[], name: string, args: Record<string, unknown>): Promise<string> {
  const tool = tools.find((t) => t.name === name);
  if (!tool) return `工具 ${name} 不可用`;
  const out = await (tool as unknown as { invoke: (a: unknown) => Promise<unknown> }).invoke(args);
  return typeof out === 'string' ? out : JSON.stringify(out);
}

/** bridge POST /internal/policy/confirm {todo_id, decision:'approve'} → {credential}。 */
async function confirmCredential(bridgeUrl: string, token: string, todoId: string): Promise<string> {
  const res = await fetch(`${bridgeUrl}/internal/policy/confirm`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
    body: JSON.stringify({ todo_id: todoId, decision: 'approve' }),
  });
  if (!res.ok) throw new Error(`policy confirm 失败（HTTP ${res.status}）`);
  const data = (await res.json()) as { credential?: unknown };
  if (typeof data.credential !== 'string' || data.credential.length === 0) {
    throw new Error('policy confirm 未返回 credential');
  }
  return data.credential;
}

/** bridge POST /internal/memory/search：按用户话检索最小记忆包（失败返回空串不阻断）。 */
async function fetchMemory(bridgeUrl: string, token: string, text: string): Promise<string> {
  try {
    const res = await fetch(`${bridgeUrl}/internal/memory/search`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
      body: JSON.stringify({ query: text, limit: 3 }),
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return '';
    const data = (await res.json()) as { records?: Array<{ content?: unknown }> };
    const lines = (data.records ?? [])
      .map((r) => `- ${String(r.content ?? '')}`.slice(0, 520))
      .slice(0, 3);
    return lines.join('\n').slice(0, 1500);
  } catch {
    return '';
  }
}

function toOpenAIUsage(u: {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}): { prompt_tokens: number; completion_tokens: number; total_tokens: number } {
  return { prompt_tokens: u.input_tokens, completion_tokens: u.output_tokens, total_tokens: u.total_tokens };
}

// ---- 进程级默认依赖（测试/启动时全部可用 opts 覆盖）----

let cachedStore: ConversationStore | null = null;
let cachedRuntime: RuntimeConfig | null = null;
let cachedModel: BaseChatModel | null = null;

function defaultStore(): ConversationStore {
  cachedStore ??= new ConversationStore();
  return cachedStore;
}

function defaultRuntime(): RuntimeConfig {
  cachedRuntime ??= loadRuntimeConfig();
  return cachedRuntime;
}

function defaultModel(): BaseChatModel {
  cachedModel ??= getChatModel('main');
  return cachedModel;
}

/** 供测试清进程级缓存（换配置目录等场景）。 */
export function resetAgentDefaults(): void {
  cachedStore = null;
  cachedRuntime = null;
  cachedModel = null;
}

// ---- 主流程 ----

/**
 * 处理一轮语音对话：流式产出回复增量，落会话历史。
 * 即使调用方提前不再消费，内部也会跑完整轮（生成器被 GC 时中止，属调用方责任）。
 */
export async function* handleVoiceTurn(
  sessionKey: string,
  userText: string,
  speaker?: string,
  opts: VoiceTurnOptions = {},
): AsyncGenerator<VoiceChunk> {
  const store = opts.store ?? defaultStore();
  const bridgeUrl = opts.bridgeUrl ?? defaultRuntime().bridgeUrl;
  const token = opts.token ?? defaultRuntime().token;
  const model = opts.model ?? defaultModel();
  const tools = (opts.toolsFactory ?? ((d: { bridgeUrl: string; token: string }) =>
    toLangChainTools({ mainVisible: true }, d)))({ bridgeUrl, token });
  // Phase 4：按需检索最小记忆包（≤3 条 × 500 字），检索失败/超时都不阻断语音轮
  const memory = await fetchMemory(bridgeUrl, token, userText);

  const system = compileRole('main', {
    CHANNEL: '语音',
    SPEAKER: speaker || '未知',
    CONTEXT: '', // 语音轮时间在用户消息的「[语音 …]」标记里，模板 voice-rules 已声明
    MEMORY: memory, // Phase 4：bridge /internal/memory/search 最小包
  }).system;

  const session = store.getSession(sessionKey);
  const pending = session.pending;
  if (pending) {
    const intent = matchIntent(userText);
    if (intent === 'affirm') {
      yield* runAffirmFlow(sessionKey, userText, pending, { model, store, system, tools, bridgeUrl, token });
      return;
    }
    if (intent === 'deny') {
      store.setPending(sessionKey, null);
      store.appendMessage(sessionKey, new HumanMessage(userText));
      store.appendMessage(
        sessionKey,
        new SystemMessage(`用户拒绝了刚才的确认请求（${pending.capabilityId}），不要再尝试该操作，简短回应即可。`),
      );
      yield* runModelLoop(sessionKey, { model, store, system, tools });
      return;
    }
    // 既非确认也非拒绝：当普通消息处理，pending 保留
  }

  store.appendMessage(sessionKey, new HumanMessage(userText));
  yield* runModelLoop(sessionKey, { model, store, system, tools });
}

interface LoopDeps {
  model: BaseChatModel;
  store: ConversationStore;
  system: string;
  tools: RuntimeTool[];
  bridgeUrl?: string;
  token?: string;
}

/** 模型循环：流式聚合 → 落 AIMessage → 执行 tool_calls → ToolMessage 回灌，直到无工具或超轮数。 */
async function* runModelLoop(sessionKey: string, deps: LoopDeps): AsyncGenerator<VoiceChunk> {
  const { model, store, system, tools } = deps;
  const bound = tools.length > 0 && model.bindTools ? model.bindTools(tools as never) : model;
  let rounds = 0;
  while (true) {
    const messages: BaseMessage[] = [new SystemMessage(system), ...store.getSession(sessionKey).messages];
    let agg: AIMessageChunk | undefined;
    for await (const c of await bound.stream(messages as never)) {
      const chunk = c as AIMessageChunk;
      agg = agg ? agg.concat(chunk) : chunk;
      const text = typeof chunk.content === 'string' ? chunk.content : '';
      if (text) yield { type: 'delta', text };
    }
    const ai = new AIMessage({
      content: agg && typeof agg.content === 'string' ? agg.content : '',
      tool_calls: (agg?.tool_calls ?? []) as AIMessage['tool_calls'],
    });
    store.appendMessage(sessionKey, ai);
    const usage = agg?.usage_metadata;
    const toolCalls = ai.tool_calls ?? [];
    if (toolCalls.length === 0 || rounds >= MAX_TOOL_ROUNDS) {
      if (usage) yield { type: 'usage', usage: toOpenAIUsage(usage) };
      return;
    }
    rounds += 1;
    for (const tc of toolCalls) {
      yield* execToolCall(sessionKey, tc, tools, store);
    }
  }
}

/** 执行一个 tool_call 并落 ToolMessage；确认类错误落 pendingApproval。 */
async function* execToolCall(
  sessionKey: string,
  tc: { id?: string; name: string; args?: Record<string, unknown> },
  tools: RuntimeTool[],
  store: ConversationStore,
): AsyncGenerator<VoiceChunk> {
  let result: string;
  try {
    result = await invokeTool(tools, tc.name, (tc.args ?? {}) as Record<string, unknown>);
  } catch (e) {
    if (isPolicyConfirmationError(e)) {
      store.setPending(sessionKey, {
        capabilityId: e.capabilityId,
        todoId: e.todoId,
        summary: e.summary,
        toolName: tc.name,
        args: (tc.args ?? {}) as Record<string, unknown>,
      });
      result = `此操作被权限系统拦下，需要主人确认后才能执行：${e.summary}。请用一句口语问主人是否允许。`;
    } else {
      result = `工具执行失败：${e instanceof Error ? e.message : String(e)}`;
    }
  }
  store.appendMessage(sessionKey, new ToolMessage({ content: result, tool_call_id: tc.id ?? '' }));
}

/** 用户确认后的重试流：confirm 换 credential → 相同参数重试 → 模型收尾。 */
async function* runAffirmFlow(
  sessionKey: string,
  userText: string,
  pending: PendingApproval,
  deps: Required<LoopDeps>,
): AsyncGenerator<VoiceChunk> {
  const { store, system, tools, bridgeUrl, token } = deps;

  store.appendMessage(sessionKey, new HumanMessage(userText));
  store.appendMessage(
    sessionKey,
    new SystemMessage(
      `用户已口头确认「${pending.capabilityId}」（${pending.summary}）。系统正在用完全相同的参数重试该操作，结果见下方工具返回；请用一句话向用户交代结果。`,
    ),
  );

  const retryCallId = `retry_${pending.todoId}`;
  const retryArgs: Record<string, unknown> = { ...pending.args };
  let result: string;
  try {
    const credential = await confirmCredential(bridgeUrl, token, pending.todoId);
    retryArgs.credential = credential;
    store.appendMessage(
      sessionKey,
      new AIMessage({ content: '', tool_calls: [{ id: retryCallId, name: pending.toolName, args: retryArgs }] }),
    );
    store.setPending(sessionKey, null);
    try {
      result = await invokeTool(tools, pending.toolName, retryArgs);
    } catch (e) {
      if (isPolicyConfirmationError(e)) {
        store.setPending(sessionKey, { ...pending, todoId: e.todoId });
        result = `重试仍需要确认：${e.summary}`;
      } else {
        result = `工具执行失败：${e instanceof Error ? e.message : String(e)}`;
      }
    }
  } catch (e) {
    // confirm 拿不到 credential（如 409 已被处理）：不重试，把结果如实交给模型收尾
    store.appendMessage(
      sessionKey,
      new AIMessage({ content: '', tool_calls: [{ id: retryCallId, name: pending.toolName, args: retryArgs }] }),
    );
    store.setPending(sessionKey, null);
    result = `确认未通过，操作未执行：${e instanceof Error ? e.message : String(e)}`;
  }
  store.appendMessage(sessionKey, new ToolMessage({ content: result, tool_call_id: retryCallId }));

  yield* runModelLoop(sessionKey, { model: deps.model, store, system, tools });
}
