import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import Fastify, { type FastifyInstance } from 'fastify';
import { handleVoiceTurn, type VoiceChunk } from '../../src/main-agent/agent.js';
import { ConversationStore } from '../../src/main-agent/conversation.js';
import { mockToolState, resetMockToolState, toLangChainTools as mockTools } from '../../src/main-agent/tool-mock.js';
import { ScriptedChatModel } from '../helpers/scripted-model.js';

let dir: string;
let bridge: FastifyInstance;
let bridgeUrl: string;
let confirmBodies: Array<{ todo_id?: string; decision?: string }>;

beforeEach(async () => {
  dir = mkdtempSync(path.join(tmpdir(), 'guanjia-agent-'));
  resetMockToolState();
  confirmBodies = [];
  bridge = Fastify();
  bridge.post('/internal/policy/confirm', async (req, reply) => {
    confirmBodies.push(req.body as { todo_id?: string; decision?: string });
    const body = req.body as { decision?: string };
    if (body?.decision === 'deny') {
      return reply.status(409).send({ error: { message: '已处理' } });
    }
    return { credential: 'cred-test-1' };
  });
  await bridge.listen({ port: 0, host: '127.0.0.1' });
  bridgeUrl = `http://127.0.0.1:${(bridge.server.address() as { port: number }).port}`;
});

afterEach(async () => {
  await bridge.close();
  rmSync(dir, { recursive: true, force: true });
});

async function collect(
  gen: AsyncIterable<VoiceChunk>,
): Promise<{ text: string; usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } }> {
  let text = '';
  let usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | undefined;
  for await (const ev of gen) {
    if (ev.type === 'delta') text += ev.text;
    else usage = ev.usage;
  }
  return { text, usage };
}

describe('确认流（pendingApproval → 口头确认 → credential 重试）', () => {
  it('工具抛 PolicyConfirmationError：回复确认话术、pending 建立、工具未被重试', async () => {
    mockToolState.throwConfirmation = true;
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'mock_tool', args: { action: 'open_browser' } }] },
      { content: '要打开浏览器，需要我确认吗？' },
    ]);
    const store = new ConversationStore(dir);

    const r1 = await collect(
      handleVoiceTurn('s1', '[语音 2026-11-05 10:00 周四 主人] 打开浏览器', '主人', { model, store, bridgeUrl, token: 'bridge-token', toolsFactory: (deps) => mockTools({}, deps) }),
    );
    expect(r1.text).toContain('需要我确认');
    expect(store.getSession('s1').pending).toMatchObject({ capabilityId: 'mock_capability', todoId: 'todo-mock-1', summary: '打开浏览器', toolName: 'mock_tool', args: { action: 'open_browser' } });
    expect(mockToolState.calls).toHaveLength(1);
    expect(mockToolState.calls[0].args).not.toHaveProperty('credential');
  });

  it('下一轮说「允许」：confirm 换 credential、相同参数带 credential 重试、模型交代结果、pending 清空', async () => {
    mockToolState.throwConfirmation = true;
    const store = new ConversationStore(dir);
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'mock_tool', args: { action: 'open_browser' } }] },
      { content: '要打开浏览器，需要我确认吗？' },
      { content: '已经打开了。', usage_metadata: { input_tokens: 100, output_tokens: 8, total_tokens: 108 } },
    ]);

    await collect(handleVoiceTurn('s1', '[语音 2026-11-05 10:00 周四 主人] 打开浏览器', '主人', { model, store, bridgeUrl, token: 'bridge-token', toolsFactory: (deps) => mockTools({}, deps) }));

    // 第二轮：口头确认
    mockToolState.throwConfirmation = false;
    mockToolState.reply = '浏览器已打开';
    const r2 = await collect(handleVoiceTurn('s1', '允许', '主人', { model, store, bridgeUrl, token: 'bridge-token', toolsFactory: (deps) => mockTools({}, deps) }));

    expect(confirmBodies).toEqual([{ todo_id: 'todo-mock-1', decision: 'approve' }]);
    expect(mockToolState.calls).toHaveLength(2);
    expect(mockToolState.calls[1].args).toEqual({ action: 'open_browser', credential: 'cred-test-1' });
    expect(r2.text).toContain('已经打开');
    expect(r2.usage).toEqual({ prompt_tokens: 100, completion_tokens: 8, total_tokens: 108 });
    expect(store.getSession('s1').pending).toBeNull();
  });

  it('下一轮说「算了」：丢弃 pending、不重试工具、模型简短回应', async () => {
    mockToolState.throwConfirmation = true;
    const store = new ConversationStore(dir);
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'mock_tool', args: { action: 'open_browser' } }] },
      { content: '要打开浏览器，需要我确认吗？' },
      { content: '好的，那先不弄了。' },
    ]);
    await collect(handleVoiceTurn('s1', '[语音 2026-11-05 10:00 周四 主人] 打开浏览器', '主人', { model, store, bridgeUrl, token: 'bridge-token', toolsFactory: (deps) => mockTools({}, deps) }));

    mockToolState.throwConfirmation = false;
    const r2 = await collect(handleVoiceTurn('s1', '算了', '主人', { model, store, bridgeUrl, token: 'bridge-token', toolsFactory: (deps) => mockTools({}, deps) }));

    expect(mockToolState.calls).toHaveLength(1); // 没有重试
    expect(confirmBodies).toHaveLength(0); // 没调 confirm
    expect(r2.text).toContain('先不弄');
    expect(store.getSession('s1').pending).toBeNull();
  });

  it('下一轮既非确认也非拒绝：当普通消息处理，pending 保留', async () => {
    mockToolState.throwConfirmation = true;
    const store = new ConversationStore(dir);
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'mock_tool', args: { action: 'open_browser' } }] },
      { content: '要打开浏览器，需要我确认吗？' },
      { content: '北京今天晴。' },
    ]);
    await collect(handleVoiceTurn('s1', '[语音 2026-11-05 10:00 周四 主人] 打开浏览器', '主人', { model, store, bridgeUrl, token: 'bridge-token', toolsFactory: (deps) => mockTools({}, deps) }));

    const r2 = await collect(handleVoiceTurn('s1', '北京天气怎么样', '主人', { model, store, bridgeUrl, token: 'bridge-token', toolsFactory: (deps) => mockTools({}, deps) }));
    expect(r2.text).toContain('晴');
    expect(store.getSession('s1').pending).not.toBeNull(); // pending 保留
  });
});

describe('普通工具循环', () => {
  it('tool_call → 结果回灌 → 模型继续，历史含 AIMessage(tool_calls) 与 ToolMessage', async () => {
    mockToolState.reply = '联盟已启动';
    const store = new ConversationStore(dir);
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'mock_tool', args: { action: 'launch' } }] },
      { content: '开好了，去玩吧。' },
    ]);
    const r = await collect(handleVoiceTurn('s1', '[语音 2026-11-05 10:00 周四] 开个联盟', undefined, { model, store, bridgeUrl, token: 't', toolsFactory: (deps) => mockTools({}, deps) }));
    expect(r.text).toBe('开好了，去玩吧。');
    const msgs = store.getSession('s1').messages;
    expect(msgs[0]).toBeInstanceOf(Object);
    const aiWithCalls = msgs.find((m) => m.getType?.() === 'ai' && (m as { tool_calls?: unknown[] }).tool_calls?.length);
    expect(aiWithCalls).toBeDefined();
    const toolMsg = msgs.find((m) => m.getType?.() === 'tool');
    expect(toolMsg).toBeDefined();
    expect((toolMsg as { content: unknown }).content).toBe('联盟已启动');
  });
});
