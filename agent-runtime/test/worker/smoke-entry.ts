/**
 * 冒烟入口（Wave1-C 端到端，用后即删）：独立 fastify 实例 + registerWorkerRoutes。
 *
 * src/server/app.ts 尚未接线 worker 路由（并行限制，主会话统一加），
 * 本文件冒烟时以 npx tsx 启动，验证真实 bridge + 真模型 + 真工具全链路。
 * 运行：npx tsx test/worker/smoke-entry.ts
 */
import Fastify from 'fastify';
import { loadRuntimeConfig } from '../../src/config.js';
import { registerWorkerRoutes } from '../../src/worker/routes.js';

const cfg = loadRuntimeConfig();
if (!cfg.token) {
  console.error('缺少 token（agent-runtime/config/runtime.json 或 AGENT_RUNTIME_TOKEN）');
  process.exit(1);
}

const app = Fastify({ logger: false });
app.addHook('onRequest', async (req, reply) => {
  if (req.url.split('?')[0] === '/healthz') return;
  if (req.headers.authorization !== `Bearer ${cfg.token}`) {
    reply.status(401).send({ error: { message: '无效的访问令牌', type: 'invalid_request_error' } });
    return reply;
  }
});
app.get('/healthz', async () => ({ ok: true }));

registerWorkerRoutes(app, {
  token: cfg.token,
  bridgeUrl: cfg.bridgeUrl,
  callbackBase: `http://127.0.0.1:${cfg.port}`,
});

await app.listen({ port: cfg.port, host: '127.0.0.1' });
console.log(`worker smoke listening on http://127.0.0.1:${cfg.port}（bridge ${cfg.bridgeUrl}）`);

let closing = false;
const shutdown = (): void => {
  if (closing) return;
  closing = true;
  app.close().finally(() => process.exit(0));
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
