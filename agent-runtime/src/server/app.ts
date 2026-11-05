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
import Fastify, { type FastifyInstance } from 'fastify';
import { handleVoiceTurn, type VoiceTurnOptions } from '../main-agent/agent.js';
import { compileRole } from '../prompts/compiler.js';
import { registerWorkerRoutes } from '../worker/routes.js';

export interface AppOptions {
  token: string;
  /** 透传给 handleVoiceTurn（测试注入假模型/临时会话目录等）。 */
  agentOpts?: VoiceTurnOptions;
  logger?: boolean;
}

function headerStr(v: unknown): string | undefined {
  return typeof v === 'string' && v.length > 0 ? v : undefined;
}

export function buildApp(opts: AppOptions): FastifyInstance {
  const app = Fastify({ logger: opts.logger ?? false });

  app.addHook('onRequest', async (req, reply) => {
    if (req.url.split('?')[0] === '/healthz') return;
    if (req.headers.authorization !== `Bearer ${opts.token}`) {
      reply.status(401).send({ error: { message: '无效的访问令牌', type: 'invalid_request_error' } });
      return reply;
    }
  });

  app.get('/healthz', async () => ({ ok: true }));

  // Wave1-C：Worker Runtime 路由（/v1/worker/*，Python Task Engine 经反转后的适配器调用）。
  // 鉴权复用全局 token；run 管理器的 checkpoint/bridge 地址走 runtime.json 默认值。
  registerWorkerRoutes(app, { token: opts.token });

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

    const turn = handleVoiceTurn(sessionKey, userText, speaker, opts.agentOpts);

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
      try {
        for await (const ev of turn) {
          if (ev.type === 'delta') {
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
        send({
          id: completionId,
          object: 'chat.completion.chunk',
          created,
          model: modelName,
          choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
          ...(usage ? { usage } : {}),
        });
      } catch (e) {
        send({ error: { message: e instanceof Error ? e.message : String(e), type: 'runtime_error' } });
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
      return reply
        .status(500)
        .send({ error: { message: e instanceof Error ? e.message : String(e), type: 'runtime_error' } });
    }
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
