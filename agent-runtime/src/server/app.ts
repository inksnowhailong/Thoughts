/**
 * OpenAI 兼容语音入口（fastify）。
 *
 * - POST /v1/chat/completions：只取最后一条 role=user 的 content 当 userText，
 *   session key 读 x-openclaw-session-key（缺省 "default"），speaker 读 x-guanjia-speaker（可缺省）；
 *   stream=true 时回 OpenAI 兼容 SSE（delta 增量 → finish_reason:"stop" + usage → [DONE]），
 *   即使客户端断开也把模型流读完（Python 侧 barge-in 只停播放、流要读到 [DONE]，契约不破坏）；
 *   stream=false 整段返回 choices[0].message.content；
 * - GET /internal/diag/prompt-report：main 角色 prompt 分节字符数与 token 估算；
 * - GET /healthz：免鉴权健康检查；
 * - 统一鉴权：除 /healthz 外校验 Authorization: Bearer <token>（401）；
 * - 错误统一 OpenAI 格式 {error:{message,type}}（brain.py 按该形状解析）。
 */
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';
import { handleVoiceTurn, type VoiceTurnOptions } from '../main-agent/agent.js';
import { compileRole } from '../prompts/compiler.js';
import { MemoryEngine } from '../memory-engine/service.js';
import { registerMemoryRoutes } from '../memory-engine/routes.js';
import { ObservabilityHub, type RuntimeEvent } from '../observability/runtime.js';
import { AGENT_RUNTIME_ROOT } from '../tools/paths.js';
import { registerWorkerRoutes } from '../worker/routes.js';

export interface AppOptions {
  token: string;
  /** 透传给 handleVoiceTurn（测试注入假模型/临时会话目录等）。 */
  agentOpts?: VoiceTurnOptions;
  memoryEngine?: MemoryEngine;
  observability?: ObservabilityHub;
  logger?: boolean;
}

function headerStr(v: unknown): string | undefined {
  return typeof v === 'string' && v.length > 0 ? v : undefined;
}

function cookieValue(cookieHeader: string | undefined, name: string): string | undefined {
  const entry = cookieHeader?.split(';').map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`));
  if (!entry) return undefined;
  try {
    return decodeURIComponent(entry.slice(name.length + 1));
  } catch {
    return undefined;
  }
}

export function buildApp(opts: AppOptions): FastifyInstance {
  const app = Fastify({ logger: opts.logger ?? false });
  const observability = opts.observability ?? new ObservabilityHub(':memory:');

  app.addHook('onRequest', async (req, reply) => {
    const requestPath = req.url.split('?')[0];
    if (requestPath === '/healthz' || requestPath === '/mission-control') return;
    const hasBearer = req.headers.authorization === `Bearer ${opts.token}`;
    const hasPanelCookie = requestPath.startsWith('/api/observability/')
      && cookieValue(req.headers.cookie, 'mission_control') === opts.token;
    if (!hasBearer && !hasPanelCookie) {
      reply.status(401).send({ error: { message: '无效的访问令牌', type: 'invalid_request_error' } });
      return reply;
    }
  });

  app.get('/healthz', async () => ({ ok: true }));
  app.addHook('onClose', async () => {
    if (!opts.observability) observability.close();
  });

  app.get('/mission-control', async (_request, reply) => {
    const html = await readFile(path.join(AGENT_RUNTIME_ROOT, 'mission-control.html'), 'utf8');
    reply.header('cache-control', 'no-store');
    reply.header('set-cookie',
      `mission_control=${encodeURIComponent(opts.token)}; Path=/api/observability; HttpOnly; SameSite=Strict`);
    return reply.type('text/html; charset=utf-8').send(html);
  });

  app.get('/api/observability/snapshot', async () => ({
    ...observability.snapshot(Boolean(opts.memoryEngine)),
  }));
  app.get('/api/observability/traces', async (request, reply) => {
    const query = request.query as { limit?: string };
    const limit = query.limit === undefined ? 50 : Number(query.limit);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      return reply.code(400).send({ error: 'limit must be an integer from 1 to 100' });
    }
    return { traces: observability.listTraces(limit) };
  });
  app.get('/api/observability/traces/:traceId', async (request, reply) => {
    const params = request.params as { traceId: string };
    const trace = observability.getTrace(params.traceId);
    if (!trace) return reply.code(404).send({ error: 'trace_not_found' });
    return trace;
  });
  app.get('/api/observability/memories', async (request, reply) => {
    if (!opts.memoryEngine) return reply.code(503).send({ error: 'memory_engine_not_configured' });
    const query = request.query as { limit?: string; offset?: string; q?: string; type?: string };
    const limit = query.limit === undefined ? 50 : Number(query.limit);
    const offset = query.offset === undefined ? 0 : Number(query.offset);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100
      || !Number.isInteger(offset) || offset < 0) {
      return reply.code(400).send({ error: 'invalid_memory_page' });
    }
    return opts.memoryEngine.browseMemories({
      limit,
      offset,
      query: query.q,
      memoryType: query.type,
    });
  });
  app.get('/api/observability/memories/:memoryId', async (request, reply) => {
    if (!opts.memoryEngine) return reply.code(503).send({ error: 'memory_engine_not_configured' });
    const params = request.params as { memoryId: string };
    const memory = await opts.memoryEngine.getMemoryDetail(params.memoryId);
    if (!memory) return reply.code(404).send({ error: 'memory_not_found' });
    return memory;
  });
  app.get('/api/observability/events', async (request, reply) => {
    const query = request.query as { after?: string };
    const lastEventId = headerStr(request.headers['last-event-id']);
    const cursor = Number(lastEventId ?? query.after ?? '0');
    if (!Number.isSafeInteger(cursor) || cursor < 0) {
      return reply.code(400).send({ error: 'after must be a non-negative integer' });
    }

    reply.hijack();
    const raw = reply.raw;
    raw.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    });
    let lastSent = cursor;
    let closed = false;
    const send = (event: RuntimeEvent): void => {
      if (closed || event.hubOffset <= lastSent) return;
      lastSent = event.hubOffset;
      raw.write(`id: ${event.hubOffset}\nevent: runtime\ndata: ${JSON.stringify(event)}\n\n`);
    };
    const unsubscribe = observability.subscribe(send);
    const cleanup = (): void => {
      if (closed) return;
      closed = true;
      clearInterval(keepalive);
      unsubscribe();
    };
    const keepalive = setInterval(() => {
      if (!closed) raw.write(': keepalive\n\n');
    }, 15_000);
    keepalive.unref();
    raw.on('close', cleanup);
    raw.write(': connected\n\n');

    const replay = observability.eventsSince(cursor);
    for (const event of replay.events) send(event);
    if (replay.hasMore) {
      raw.write(`event: resync_required\ndata: ${JSON.stringify({ watermark: lastSent })}\n\n`);
      cleanup();
      raw.end();
    }
    return reply;
  });

  // Wave1-C：Worker Runtime 路由（/v1/worker/*，Python Task Engine 经反转后的适配器调用）。
  // 鉴权复用全局 token；run 管理器的 checkpoint/bridge 地址走 runtime.json 默认值。
  registerWorkerRoutes(app, { token: opts.token, memoryEngine: opts.memoryEngine });
  registerMemoryRoutes(app, opts.memoryEngine);

  app.get('/internal/diag/prompt-report', async () => {
    const { sections } = compileRole('main', { CHANNEL: '', SPEAKER: '', CONTEXT: '', MEMORY: '' });
    const totalChars = sections.reduce((n, s) => n + s.chars, 0);
    return { main: { sections, tokensEstimate: Math.ceil(totalChars / 2) } };
  });

  app.post('/v1/chat/completions', async (req, reply) => {
    const body = (req.body ?? {}) as { model?: unknown; messages?: unknown; stream?: unknown };
    const messages = Array.isArray(body.messages) ? (body.messages as Array<{ role?: unknown; content?: unknown }>) : [];
    const lastUser = [...messages].reverse().find((m) => m?.role === 'user');
    if (!lastUser || typeof lastUser.content !== 'string') {
      return reply
        .status(400)
        .send({ error: { message: 'messages 里找不到 role=user 的消息', type: 'invalid_request_error' } });
    }
    const userText = lastUser.content;
    const sessionKey = headerStr(req.headers['x-openclaw-session-key']) ?? 'default';
    const speaker = headerStr(req.headers['x-guanjia-speaker']);
    const completionId = `chatcmpl-${randomUUID()}`;
    const modelName = typeof body.model === 'string' && body.model.length > 0 ? body.model : 'main';
    const created = Math.floor(Date.now() / 1000);
    const trace = observability.startTrace({
      sessionId: sessionKey,
      userText,
      model: modelName,
    });

    const turn = handleVoiceTurn(sessionKey, userText, speaker, {
      ...opts.agentOpts,
      ...(opts.memoryEngine ? { memoryEngine: opts.memoryEngine } : {}),
      observability: trace,
    });

    if (body.stream === true) {
      reply.hijack();
      const raw = reply.raw;
      raw.writeHead(200, {
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache',
        connection: 'keep-alive',
      });
      const send = (obj: unknown): void => {
        try {
          raw.write(`data: ${JSON.stringify(obj)}\n\n`);
        } catch {
          // 客户端断开也要继续把模型流读完（barge-in 契约）
        }
      };
      let usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | undefined;
      let output = '';
      try {
        for await (const ev of turn) {
          if (ev.type === 'delta') {
            output += ev.text;
            send({
              id: completionId,
              object: 'chat.completion.chunk',
              created,
              model: modelName,
              choices: [{ index: 0, delta: { content: ev.text }, finish_reason: null }],
            });
          } else {
            usage = ev.usage;
          }
        }
        trace.finish('completed', { output, usage: usage ?? null, model: modelName });
        send({
          id: completionId,
          object: 'chat.completion.chunk',
          created,
          model: modelName,
          choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
          ...(usage ? { usage } : {}),
        });
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        trace.finish('failed', { output, model: modelName }, message);
        send({ error: { message, type: 'runtime_error' } });
      }
      raw.write('data: [DONE]\n\n');
      raw.end();
      return reply;
    }

    // 非流式：整段聚合（同样把流读完）
    let content = '';
    let usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | undefined;
    try {
      for await (const ev of turn) {
        if (ev.type === 'delta') content += ev.text;
        else usage = ev.usage;
      }
    } catch (e) {
      trace.finish('failed', { output: content, model: modelName }, e instanceof Error ? e.message : String(e));
      return reply
        .status(500)
        .send({ error: { message: e instanceof Error ? e.message : String(e), type: 'runtime_error' } });
    }
    trace.finish('completed', { output: content, usage: usage ?? null, model: modelName });
    return {
      id: completionId,
      object: 'chat.completion',
      created,
      model: modelName,
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
      ...(usage ? { usage } : {}),
    };
  });

  app.setErrorHandler((err: Error & { statusCode?: number }, _req, reply) => {
    const status = typeof err.statusCode === 'number' && err.statusCode >= 400 ? err.statusCode : 500;
    reply
      .status(status)
      .send({ error: { message: err.message, type: status >= 500 ? 'runtime_error' : 'invalid_request_error' } });
  });

  return app;
}
