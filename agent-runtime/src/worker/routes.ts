/**
 * Worker 路由插件（Wave1-C）：
 *   POST /v1/worker/runs               创建并立即后台执行
 *   GET  /v1/worker/runs/:id/events    SSE 事件流（?since=seq 断线续传，信封 {seq, event}）
 *   POST /v1/worker/runs/:id/resume    审批恢复（approve 附一次性 credential / reject）
 *   POST /v1/worker/runs/:id/cancel    取消（置标记；interrupt 挂起时按 reject 唤醒）
 *
 * 鉴权：opts.token 提供时对 /v1/worker/* 校验 Bearer（与主服务全局鉴权同 token，重复校验幂等）。
 * app.ts 接线：`registerWorkerRoutes(app, { token: opts.token })`。
 */
import type { FastifyInstance, FastifyReply } from 'fastify';
import { ZodError } from 'zod';
import { HttpError, WorkerRunManager, isTerminal, type WorkerRuntimeOptions } from './runs.js';

export interface WorkerRoutesOptions extends WorkerRuntimeOptions {
  token?: string;
  /** 测试注入共享 manager；缺省新建（用 loadRuntimeConfig 的 bridgeUrl/token）。 */
  manager?: WorkerRunManager;
}

function sendError(reply: FastifyReply, e: unknown): FastifyReply {
  if (e instanceof ZodError) {
    const message = e.issues.map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`).join('; ');
    return reply.status(400).send({ error: { message, type: 'invalid_request_error' } });
  }
  if (e instanceof HttpError) {
    return reply.status(e.statusCode).send({ error: { message: e.message, type: 'invalid_request_error' } });
  }
  return reply.status(500).send({
    error: { message: e instanceof Error ? e.message : String(e), type: 'runtime_error' },
  });
}

const SSE_POLL_MS = 100;

export function registerWorkerRoutes(app: FastifyInstance, opts: WorkerRoutesOptions = {}): void {
  const manager = opts.manager ?? new WorkerRunManager(opts);

  if (opts.token) {
    app.addHook('onRequest', async (req, reply) => {
      if (!req.url.split('?')[0].startsWith('/v1/worker/')) return;
      if (req.headers.authorization !== `Bearer ${opts.token}`) {
        reply.status(401).send({ error: { message: '无效的访问令牌', type: 'invalid_request_error' } });
        return reply;
      }
    });
  }

  app.post('/v1/worker/runs', async (req, reply) => {
    try {
      return manager.createRun(req.body);
    } catch (e) {
      return sendError(reply, e);
    }
  });

  app.get('/v1/worker/runs/:id/events', async (req, reply) => {
    const { id } = req.params as { id: string };
    const query = (req.query ?? {}) as { since?: unknown };
    // ?since=N：客户端已收到 seq<=N，续传 seq>N；未提供时全量从 seq=0 开始。
    const sinceProvided = typeof query.since === 'string' && /^\d+$/.test(query.since);
    const since = sinceProvided ? Number(query.since) : -1;
    const record = manager.getRecord(id);
    if (!record) return sendError(reply, new HttpError(404, `worker run ${id} 不存在`));
    const status = manager.recordStatus(id)!;

    reply.hijack();
    const raw = reply.raw;
    raw.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    });
    let closed = false;
    raw.on('error', () => {
      closed = true;
    });
    let cursor = since + 1; // cursor = 下一个要发的 seq；?since=N 表示客户端已收到 seq<=N
    try {
      while (!closed) {
        const current = manager.getRecord(id);
        if (!current) break;
        const events = manager.eventsSince(id, cursor - 1);
        for (const stored of events) {
          raw.write(`data: ${JSON.stringify({ seq: stored.seq, event: stored.event })}\n\n`);
          cursor = stored.seq + 1;
        }
        if (isTerminal(manager.recordStatus(id) ?? status)) break;
        await new Promise((resolve) => setTimeout(resolve, SSE_POLL_MS));
      }
    } catch {
      // 客户端断开：直接结束
    }
    raw.end();
    return reply;
  });

  app.post('/v1/worker/runs/:id/resume', async (req, reply) => {
    const { id } = req.params as { id: string };
    try {
      return await manager.resume(id, req.body);
    } catch (e) {
      return sendError(reply, e);
    }
  });

  app.post('/v1/worker/runs/:id/cancel', async (req, reply) => {
    const { id } = req.params as { id: string };
    try {
      return await manager.cancel(id, req.body);
    } catch (e) {
      return sendError(reply, e);
    }
  });
}
