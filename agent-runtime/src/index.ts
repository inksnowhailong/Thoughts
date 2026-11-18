/**
 * agent-runtime 入口：启动 OpenAI 兼容语音入口服务（仅本机回环）。
 * 配置来自 config/runtime.json（env 可覆盖），鉴权 token 与语音侧 GUANJIA_GATEWAY_TOKEN 同值。
 */
import { loadRuntimeConfig } from './config.js';
import { MemoryEngine } from './memory-engine/service.js';
import { ObservabilityHub } from './observability/runtime.js';
import { buildApp } from './server/app.js';

export const AGENT_RUNTIME_VERSION = '0.1.0';

async function main(): Promise<void> {
  const cfg = loadRuntimeConfig();
  if (!cfg.token) {
    console.error('缺少 token：在 agent-runtime/config/runtime.json 里配置，或设置 AGENT_RUNTIME_TOKEN');
    process.exit(1);
  }
  const observability = new ObservabilityHub();
  const memoryEngine = new MemoryEngine(undefined, undefined, observability);
  await memoryEngine.initialize();
  const app = buildApp({ token: cfg.token, logger: false, memoryEngine, observability });
  await app.listen({ port: cfg.port, host: '127.0.0.1' });
  console.log(`agent-runtime ${AGENT_RUNTIME_VERSION} 监听 http://127.0.0.1:${cfg.port}（bridge ${cfg.bridgeUrl}）`);

  let closing = false;
  const shutdown = (): void => {
    if (closing) return;
    closing = true;
    app.close().then(async () => {
      await memoryEngine.close();
      observability.close();
      process.exit(0);
    }).catch((error: unknown) => {
      console.error('Agent Runtime shutdown failed:', error);
      process.exit(1);
    });
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

await main();
