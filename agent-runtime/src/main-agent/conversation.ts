/**
 * Main Agent 会话管理。
 *
 * - 按 session key（请求头 x-openclaw-session-key，缺省 "default"）隔离会话；
 * - 消息历史持久化到 data/agent-runtime/sessions/<key>.jsonl（追加，保留全量）；
 * - 内存只保留最近 MAX_ROUNDS 轮（一轮 = 一条 HumanMessage 到下一条 HumanMessage 前），
 *   超龄轮次直接丢弃（不做摘要）；首次访问时从 jsonl 尾部加载同样多的轮次；
 * - pendingApproval（工具确认挂起）随 jsonl 一起持久化，重启后可恢复。
 */
import { createHash } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { AIMessage, HumanMessage, SystemMessage, ToolMessage, type BaseMessage } from '@langchain/core/messages';

/** 工具被权限拦截、等待用户口头确认的挂起请求。 */
export interface PendingApproval {
  capabilityId: string;
  todoId: string;
  summary: string;
  toolName: string;
  args: Record<string, unknown>;
}

/** 内存保留的最大轮数（超龄丢弃，不摘要）。 */
export const MAX_ROUNDS = 12;

interface SessionState {
  messages: BaseMessage[];
  pending: PendingApproval | null;
}

type SerializedToolCall = { id: string; name: string; args: Record<string, unknown> };

type JournalLine =
  | { t: 'msg'; role: 'human' | 'ai' | 'system' | 'tool'; content: string; tool_calls?: SerializedToolCall[]; tool_call_id?: string }
  | { t: 'pending'; pending: PendingApproval | null };

function defaultSessionsDir(): string {
  const here =
    typeof __dirname !== 'undefined'
      ? __dirname
      : fileURLToPath(new URL('.', import.meta.url));
  // src/main-agent/ → 仓库根 data/agent-runtime/sessions
  return path.resolve(here, '..', '..', '..', 'data', 'agent-runtime', 'sessions');
}

/** session key 来自请求头，做文件名安全化（Windows 禁 `:` 等字符）。 */
function safeFileName(key: string): string {
  const encoded = encodeURIComponent(key);
  if (encoded.length <= 150) return encoded;
  const hash = createHash('sha256').update(key).digest('hex').slice(0, 12);
  return `${encoded.slice(0, 100)}-${hash}`;
}

function messageContentString(msg: BaseMessage): string {
  return typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content);
}

function toLine(msg: BaseMessage): JournalLine {
  const content = messageContentString(msg);
  if (msg instanceof HumanMessage) return { t: 'msg', role: 'human', content };
  if (msg instanceof SystemMessage) return { t: 'msg', role: 'system', content };
  if (msg instanceof ToolMessage) return { t: 'msg', role: 'tool', content, tool_call_id: msg.tool_call_id };
  const toolCalls = (msg as AIMessage).tool_calls;
  return {
    t: 'msg',
    role: 'ai',
    content,
    ...(toolCalls && toolCalls.length > 0
      ? { tool_calls: toolCalls.map((tc) => ({ id: tc.id ?? '', name: tc.name, args: (tc.args ?? {}) as Record<string, unknown> })) }
      : {}),
  };
}

function fromLine(line: JournalLine): BaseMessage | null {
  if (line.t === 'pending') return null;
  switch (line.role) {
    case 'human':
      return new HumanMessage(line.content);
    case 'system':
      return new SystemMessage(line.content);
    case 'tool':
      return new ToolMessage({ content: line.content, tool_call_id: line.tool_call_id ?? '' });
    case 'ai':
      return new AIMessage({
        content: line.content,
        ...(line.tool_calls && line.tool_calls.length > 0 ? { tool_calls: line.tool_calls } : {}),
      });
  }
}

/** 从消息（或行）序列尾部往前保留最近 MAX_ROUNDS 轮：从倒数第 MAX_ROUNDS 个 human 起、之后的全部保留。 */
function tailRounds<T>(items: T[], isHuman: (item: T) => boolean): T[] {
  let seen = 0;
  let cut = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    if (isHuman(items[i])) {
      seen += 1;
      cut = i;
      if (seen >= MAX_ROUNDS) break;
    }
  }
  return seen >= MAX_ROUNDS ? items.slice(cut) : items.slice();
}

export class ConversationStore {
  private readonly dir: string;
  private readonly sessions = new Map<string, SessionState>();

  constructor(dir?: string) {
    this.dir = dir ?? defaultSessionsDir();
  }

  getSession(key: string): SessionState {
    let s = this.sessions.get(key);
    if (!s) {
      s = this.load(key);
      this.sessions.set(key, s);
    }
    return s;
  }

  /** 追加一条消息：内存 + jsonl，追加后按 12 轮修剪内存。 */
  appendMessage(key: string, msg: BaseMessage): void {
    const s = this.getSession(key);
    s.messages.push(msg);
    s.messages = trimMessages(s.messages);
    this.appendLine(key, toLine(msg));
  }

  /** 更新（或清除）挂起确认，持久化到 jsonl。 */
  setPending(key: string, pending: PendingApproval | null): void {
    this.getSession(key).pending = pending;
    this.appendLine(key, { t: 'pending', pending });
  }

  /** 会话文件路径（测试用）。 */
  fileOf(key: string): string {
    return path.join(this.dir, `${safeFileName(key)}.jsonl`);
  }

  private appendLine(key: string, line: JournalLine): void {
    mkdirSync(this.dir, { recursive: true });
    appendFileSync(this.fileOf(key), `${JSON.stringify(line)}\n`, 'utf8');
  }

  private load(key: string): SessionState {
    const file = this.fileOf(key);
    if (!existsSync(file)) return { messages: [], pending: null };
    const lines = readFileSync(file, 'utf8')
      .split(/\r?\n/)
      .filter((l) => l.length > 0)
      .map((l) => JSON.parse(l) as JournalLine);

    // 只取尾部 12 轮；pending 取最后一条 pending 行
    const kept = tailRounds(
      lines.filter((l) => l.t === 'msg'),
      (l) => l.t === 'msg' && l.role === 'human',
    );
    const messages = kept
      .map((l) => fromLine(l))
      .filter((m): m is BaseMessage => m !== null);
    let pending: PendingApproval | null = null;
    for (const l of lines) {
      if (l.t === 'pending') pending = l.pending;
    }
    return { messages: trimMessages(messages), pending };
  }
}

function trimMessages(messages: BaseMessage[]): BaseMessage[] {
  return tailRounds(messages, (m) => m instanceof HumanMessage);
}
