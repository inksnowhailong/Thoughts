import Fastify from 'fastify';
import { MemoryEngine } from './service.js';
import { registerMemoryRoutes } from './routes.js';
import { ObservabilityHub } from '../observability/runtime.js';

const observability = new ObservabilityHub();
const engine = new MemoryEngine(undefined, undefined, observability);
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
  app.close().then(async () => {
    await engine.close();
    observability.close();
    process.exit(0);
  }).catch((error: unknown) => {
    console.error('Memory Engine shutdown failed:', error);
    process.exit(1);
  });
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
