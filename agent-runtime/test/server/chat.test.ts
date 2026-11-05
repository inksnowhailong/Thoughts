import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ConversationStore } from '../../src/main-agent/conversation.js';
import { mockToolState, resetMockToolState, toLangChainTools as mockTools } from '../../src/main-agent/tool-mock.js';
import { buildApp } from '../../src/server/app.js';
import { ScriptedChatModel } from '../helpers/scripted-model.js';

const TOKEN = 'test-token';
let dir: string;

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'guanjia-server-'));
  resetMockToolState();
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function app(model: ScriptedChatModel) {
  return buildApp({
    token: TOKEN,
    agentOpts: {
      model,
      store: new ConversationStore(dir),
      bridgeUrl: 'http://127.0.0.1:1',
      token: 'bridge-token',
      toolsFactory: (deps) => mockTools({}, deps), // 测试注入 mock 工具（生产走真实 registry）
    },
  });
}

const auth = { authorization: `Bearer ${TOKEN}` };

describe('鉴权', () => {
  it('healthz 免鉴权', async () => {
    const r = await app(new ScriptedChatModel([])).inject({ method: 'GET', url: '/healthz' });
    expect(r.statusCode).toBe(200);
    expect(r.json()).toEqual({ ok: true });
  });

  it('缺 token / 错 token → 401 OpenAI 错误格式', async () => {
    const a = app(new ScriptedChatModel([]));
    const none = await a.inject({ method: 'POST', url: '/v1/chat/completions', payload: { messages: [] } });
    expect(none.statusCode).toBe(401);
    expect(none.json().error).toMatchObject({ type: 'invalid_request_error' });

    const wrong = await a.inject({
      method: 'POST',
      url: '/v1/chat/completions',
      headers: { authorization: 'Bearer wrong' },
      payload: { messages: [{ role: 'user', content: 'hi' }] },
    });
    expect(wrong.statusCode).toBe(401);
    expect(wrong.json().error.message).toBeTruthy();
  });

  it('prompt-report 也要鉴权', async () => {
    const r = await app(new ScriptedChatModel([])).inject({ method: 'GET', url: '/internal/diag/prompt-report' });
    expect(r.statusCode).toBe(401);
  });
});

describe('POST /v1/chat/completions 非流式', () => {
  it('整段返回 choices[0].message.content（只取最后一条 user 消息）', async () => {
    const model = new ScriptedChatModel([{ content: '你好，我在。' }]);
    const r = await app(model).inject({
      method: 'POST',
      url: '/v1/chat/completions',
      headers: auth,
      payload: {
        model: 'openclaw',
        messages: [{ role: 'system', content: '旧的' }, { role: 'user', content: '你好' }],
        stream: false,
      },
    });
    expect(r.statusCode).toBe(200);
    const body = r.json();
    expect(body.object).toBe('chat.completion');
    expect(body.id).toMatch(/^chatcmpl-/);
    expect(body.model).toBe('openclaw');
    expect(body.choices[0].message).toEqual({ role: 'assistant', content: '你好，我在。' });
    expect(body.choices[0].finish_reason).toBe('stop');
  });

  it('无 user 消息 → 400 OpenAI 错误格式', async () => {
    const r = await app(new ScriptedChatModel([])).inject({
      method: 'POST',
      url: '/v1/chat/completions',
      headers: auth,
      payload: { model: 'openclaw', messages: [{ role: 'system', content: 'x' }], stream: false },
    });
    expect(r.statusCode).toBe(400);
    expect(r.json().error.type).toBe('invalid_request_error');
  });

  it('走工具的轮次：模型继续后返回最终回复', async () => {
    mockToolState.reply = '天气：晴';
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'c1', name: 'mock_tool', args: { action: 'weather' } }] },
      { content: '今天晴，适合出门。' },
    ]);
    const r = await app(model).inject({
      method: 'POST',
      url: '/v1/chat/completions',
      headers: { ...auth, 'x-openclaw-session-key': 'agent:main:main', 'x-guanjia-speaker': '主人' },
      payload: { model: 'openclaw', messages: [{ role: 'user', content: '[语音 2026-11-05 10:00 周四 主人] 今天天气' }], stream: false },
    });
    expect(r.statusCode).toBe(200);
    expect(r.json().choices[0].message.content).toBe('今天晴，适合出门。');
    expect(mockToolState.calls).toHaveLength(1);
  });
});

describe('POST /v1/chat/completions SSE 流式', () => {
  it('chunk 结构 + finish_reason stop + usage + [DONE]，content-type 为 text/event-stream', async () => {
    const model = new ScriptedChatModel([
      { content: '你好', usage_metadata: { input_tokens: 10, output_tokens: 2, total_tokens: 12 } },
    ]);
    const r = await app(model).inject({
      method: 'POST',
      url: '/v1/chat/completions',
      headers: auth,
      payload: { model: 'openclaw', messages: [{ role: 'user', content: '你好' }], stream: true },
    });
    expect(r.statusCode).toBe(200);
    expect(r.headers['content-type']).toContain('text/event-stream');

    const events = r.body
      .split('\n\n')
      .map((l) => l.trim())
      .filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).trim());
    expect(events[events.length - 1]).toBe('[DONE]');

    const chunks = events.slice(0, -1).map((e) => JSON.parse(e) as Record<string, any>);
    expect(chunks[0].id).toMatch(/^chatcmpl-/);
    expect(chunks[0].object).toBe('chat.completion.chunk');
    expect(chunks[0].model).toBe('openclaw');
    expect(chunks[0].choices[0].delta.content).toBe('你好');
    expect(chunks[0].choices[0].finish_reason).toBeNull();

    const last = chunks[chunks.length - 1];
    expect(last.choices[0].finish_reason).toBe('stop');
    expect(last.usage).toEqual({ prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 });
  });

  it('多段增量依次产出（brain.py 逐段 yield 的契约）', async () => {
    const model = new ScriptedChatModel([{ deltas: ['今天', '晴天。'] }]);
    const r = await app(model).inject({
      method: 'POST',
      url: '/v1/chat/completions',
      headers: auth,
      payload: { model: 'openclaw', messages: [{ role: 'user', content: '天气' }], stream: true },
    });
    const contents = r.body
      .split('\n\n')
      .map((l) => l.trim())
      .filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).trim())
      .filter((e) => e !== '[DONE]')
      .map((e) => (JSON.parse(e) as { choices: Array<{ delta?: { content?: string } }> }).choices[0]?.delta?.content)
      .filter(Boolean);
    expect(contents.join('')).toBe('今天晴天。');
  });

  it('模型流中途出错：发 OpenAI 格式 error chunk 后仍以 [DONE] 收尾', async () => {
    const throwing = new ScriptedChatModel([]);
    (throwing as unknown as { _streamResponseChunks: unknown })._streamResponseChunks = async function* () {
      throw new Error('模型炸了');
    };
    const res = await buildApp({
      token: TOKEN,
      agentOpts: {
        model: throwing,
        store: new ConversationStore(dir),
        bridgeUrl: 'http://127.0.0.1:1',
        token: 't',
      },
    }).inject({
      method: 'POST',
      url: '/v1/chat/completions',
      headers: auth,
      payload: { model: 'openclaw', messages: [{ role: 'user', content: 'hi' }], stream: true },
    });
    expect(res.statusCode).toBe(200);
    expect(res.body).toContain('"error"');
    expect(res.body).toContain('模型炸了');
    expect(res.body.trim().endsWith('data: [DONE]')).toBe(true);
  });
});

describe('GET /internal/diag/prompt-report', () => {
  it('返回 main 各节字符数与 token 估算', async () => {
    const r = await app(new ScriptedChatModel([])).inject({ method: 'GET', url: '/internal/diag/prompt-report', headers: auth });
    expect(r.statusCode).toBe(200);
    const body = r.json();
    const names = body.main.sections.map((s: { name: string }) => s.name);
    expect(names).toEqual(['platform-min', 'persona', 'voice-rules', 'context', 'memory']);
    for (const s of body.main.sections) {
      expect(s.chars).toBeGreaterThan(0);
    }
    expect(body.main.tokensEstimate).toBeGreaterThan(0);
  });
});
