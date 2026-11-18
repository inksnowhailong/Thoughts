import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import Database from 'better-sqlite3';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ConversationStore } from '../../src/main-agent/conversation.js';
import { mockToolState, resetMockToolState, toLangChainTools as mockTools } from '../../src/main-agent/tool-mock.js';
import { ObservabilityHub } from '../../src/observability/runtime.js';
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

  it('面板页面建立 HttpOnly 会话 Cookie，观测 API 仍需要鉴权', async () => {
    const a = app(new ScriptedChatModel([]));
    const page = await a.inject({ method: 'GET', url: '/mission-control' });
    expect(page.statusCode).toBe(200);
    expect(page.headers['set-cookie']).toContain('HttpOnly');
    expect(page.body).toContain('实时拓扑');

    const denied = await a.inject({ method: 'GET', url: '/api/observability/snapshot' });
    expect(denied.statusCode).toBe(401);
    const allowed = await a.inject({
      method: 'GET',
      url: '/api/observability/snapshot',
      headers: { cookie: 'mission_control=test-token' },
    });
    expect(allowed.statusCode).toBe(200);
    expect(allowed.json().watermark).toBe(0);
    await a.close();
  });
});

describe('Mission Control 记忆浏览', () => {
  it('分页读取真实记忆并显示关联详情', async () => {
    const memoryDatabasePath = path.join(dir, 'memory.sqlite3');
    const memoryDatabase = new Database(memoryDatabasePath);
    memoryDatabase.exec('CREATE TABLE guanjia_memories (key TEXT PRIMARY KEY, payload TEXT NOT NULL)');
    const timestamp = new Date().toISOString();
    const memory = {
      id: 'memory-1',
      key: 'local-storage',
      content: 'The household uses SQLite for local memory.',
      memory_type: 'project_fact',
      source: 'user',
      confidence: 0.9,
      importance: 8,
      retention: 'long_term',
      people: ['household'],
      evidence: [{ reference: 'turn-1', source: 'user', observed_at: timestamp }],
      media: [],
      created_at: timestamp,
      updated_at: timestamp,
      last_used_at: null,
      use_count: 2,
    };
    memoryDatabase.prepare('INSERT INTO guanjia_memories (key, payload) VALUES (?, ?)')
      .run(memory.key, JSON.stringify(memory));
    memoryDatabase.close();
    const a = buildApp({ token: TOKEN, guanjiaMemoryDatabasePath: memoryDatabasePath });
    try {
      const page = await a.inject({
        method: 'GET',
        url: '/api/observability/memories?limit=1&offset=0&q=household&type=project_fact',
        headers: auth,
      });
      expect(page.statusCode).toBe(200);
      expect(page.json()).toMatchObject({
        total: 1,
        items: [expect.objectContaining({
          id: memory.id,
          key: memory.key,
          briefContent: memory.content,
          memoryType: memory.memory_type,
        })],
      });

      const detail = await a.inject({
        method: 'GET',
        url: `/api/observability/memories/${memory.id}`,
        headers: auth,
      });
      expect(detail.statusCode).toBe(200);
      expect(detail.json()).toMatchObject({
        id: memory.id,
        content: memory.content,
        evidence: memory.evidence,
        source: memory.source,
      });
    } finally {
      await a.close();
    }
  });
});

describe('Mission Control 观测接入', () => {
  it('内部生产者事件可重复补发且 Trace 可回放', async () => {
    const observability = new ObservabilityHub(':memory:');
    const a = buildApp({ token: TOKEN, observability });
    const traceId = 'c'.repeat(32);
    const events = [
      {
        eventId: 'python-event-1',
        producerId: 'python:test',
        producerSequence: 1,
        eventType: 'trace.started',
        timestamp: new Date().toISOString(),
        traceId,
        nodeId: 'voice-runtime',
        payload: { sessionId: 'voice-session', input: { userText: '开灯' } },
      },
      {
        eventId: 'python-event-2',
        producerId: 'python:test',
        producerSequence: 2,
        eventType: 'span.started',
        timestamp: new Date().toISOString(),
        traceId,
        spanId: 'a'.repeat(16),
        nodeId: 'asr',
        payload: { name: '语音识别', input: { text: '开灯' } },
      },
      {
        eventId: 'python-event-3',
        producerId: 'python:test',
        producerSequence: 3,
        eventType: 'span.completed',
        timestamp: new Date().toISOString(),
        traceId,
        spanId: 'a'.repeat(16),
        nodeId: 'asr',
        payload: { name: '语音识别', status: 'completed', output: { text: '开灯' }, durationMs: 42 },
      },
    ];
    try {
      for (const event of events) {
        const response = await a.inject({
          method: 'POST',
          url: '/internal/observability/events',
          headers: auth,
          payload: { events: [event] },
        });
        expect(response.statusCode).toBe(200);
        expect(response.json().accepted).toBe(1);
      }
      const duplicate = await a.inject({
        method: 'POST',
        url: '/internal/observability/events',
        headers: auth,
        payload: { events: [events[1]] },
      });
      expect(duplicate.statusCode).toBe(200);
      expect(observability.retentionStats().eventCount).toBe(3);

      const trace = await a.inject({
        method: 'GET',
        url: `/api/observability/traces/${traceId}`,
        headers: auth,
      });
      expect(trace.json().spans).toMatchObject([
        { nodeId: 'asr', status: 'completed', output: { text: '开灯' } },
      ]);
      const replay = await a.inject({
        method: 'GET',
        url: `/api/observability/traces/${traceId}/replay`,
        headers: auth,
      });
      expect(replay.json().events.map((event: { eventType: string }) => event.eventType)).toEqual([
        'trace.started', 'span.started', 'span.completed',
      ]);
    } finally {
      await a.close();
      observability.close();
    }
  });
});

describe('POST /v1/chat/completions 非流式', () => {
  it('为真实对话保存输入、模型调用、响应和 Trace 事件', async () => {
    const observability = new ObservabilityHub(':memory:');
    const a = buildApp({
      token: TOKEN,
      observability,
      agentOpts: {
        model: new ScriptedChatModel([{ content: 'hello back' }]),
        store: new ConversationStore(dir),
        bridgeUrl: 'http://127.0.0.1:1',
        token: 'bridge-token',
        toolsFactory: (deps) => mockTools({}, deps),
      },
    });
    try {
      const response = await a.inject({
        method: 'POST',
        url: '/v1/chat/completions',
        headers: auth,
        payload: { model: 'test-model', messages: [{ role: 'user', content: 'hello trace' }] },
      });
      expect(response.statusCode).toBe(200);
      const [summary] = observability.listTraces();
      const trace = observability.getTrace(summary.traceId);
      expect(trace?.input).toMatchObject({ userText: 'hello trace', model: 'test-model' });
      expect(trace?.output).toMatchObject({ output: 'hello back' });
      const modelSpan = trace?.spans.find((span) => span.nodeId === 'model-provider');
      expect(modelSpan?.input).toMatchObject({
        model: 'ScriptedChatModel',
        requestCapture: 'artifact',
        providerPayloadCapture: 'not_available',
      });
      expect(modelSpan?.output).toMatchObject({
        responseArtifact: expect.objectContaining({ artifactId: expect.any(String) }),
        captureStatus: { request: 'complete', response: 'complete' },
      });
      expect(modelSpan?.artifacts?.map((artifact) => artifact.type)).toEqual([
        'model_request',
        'model_response',
      ]);
      const requestArtifact = observability.getArtifactChunk(
        modelSpan!.artifacts![0].artifactId,
      );
      expect(JSON.parse(Buffer.from(requestArtifact!.content, 'base64').toString('utf8')))
        .toMatchObject({
          messages: expect.arrayContaining([expect.objectContaining({ content: 'hello trace' })]),
        });
      expect(observability.eventsSince(0).events.map((event) => event.eventType)).toContain('trace.completed');
    } finally {
      await a.close();
      observability.close();
    }
  });

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
