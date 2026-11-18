import Fastify from 'fastify';
import { MemoryEngine } from './service.js';
import { registerMemoryRoutes } from './routes.js';

const engine = new MemoryEngine();
await engine.initialize();

const app = Fastify({ logger: false });
app.get('/healthz', async () => ({ ok: true }));
registerMemoryRoutes(app, engine);

await app.listen({ host: '127.0.0.1', port: 18802 });
console.log('Memory Engine listening on http://127.0.0.1:18802');

let closing = false;
const shutdown = (): void => {
  if (closing) return;
  closing = true;
  app.close().finally(() => process.exit(0));
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
