/**
 * Tool Registry 单测（Wave1-B）：
 * - capabilities 元数据完整性与分级/可见性；
 * - toLangChainTools 过滤与 LangChain 工具形状；
 * - 参数校验、policy deny、confirmation 抛错、allow 执行、credential 头透传、审计上报；
 * - local-process 执行器与 builtin-vlm 的失败路径。
 * bridge 用本地 mock http server（无真实外呼）。
 */
import { createServer, type Server } from 'node:http';
import { AddressInfo } from 'node:net';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { CAPABILITIES } from '../../src/tools/capabilities.js';
import { toolResultFromDict } from '../../src/contracts/tool-result.js';
import {
  PolicyConfirmationError,
  buildLangChainTool,
  isPolicyConfirmationError,
  listCapabilities,
  toLangChainTools,
  type ToolDeps,
} from '../../src/tools/registry.js';
import type { Capability } from '../../src/tools/types.js';

// ---------- mock bridge ----------

interface RecordedRequest {
  method: string;
  path: string;
  headers: Record<string, string | string[] | undefined>;
  body: Record<string, unknown>;
}

interface MockBridgeState {
  check: { outcome: string; reason: string; todo_id?: string; summary?: string };
  exec: { ok: boolean; output?: unknown; error?: { code: string; message: string } };
  status?: number;
}

async function startMockBridge() {
  const state: MockBridgeState = {
    check: { outcome: 'allow', reason: 'mock allow' },
    exec: { ok: true, output: { text: '晴，18 度' } },
  };
  const requests: RecordedRequest[] = [];
  const server: Server = createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += String(chunk);
    });
    req.on('end', () => {
      let body: Record<string, unknown> = {};
      try {
        body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      } catch {
        body = {};
      }
      requests.push({ method: req.method ?? '', path: req.url ?? '', headers: req.headers, body });
      const send = (code: number, obj: unknown) => {
        res.writeHead(code, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(obj));
      };
      if ((req.headers.authorization ?? '') !== 'Bearer test-token') {
        send(403, { ok: false, reason: 'bad_token' });
        return;
      }
      if (req.method === 'POST' && req.url === '/internal/policy/check') {
        if (state.status !== undefined) {
          send(state.status, { ok: false });
          return;
        }
        send(200, state.check);
        return;
      }
      if (req.method === 'POST' && req.url === '/internal/audit') {
        send(200, { ok: true });
        return;
      }
      if (req.method === 'POST' && (req.url ?? '').startsWith('/internal/tools/')) {
        if (state.status !== undefined) {
          send(state.status, { ok: false });
          return;
        }
        send(200, state.exec);
        return;
      }
      send(404, { ok: false, reason: 'not_found' });
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address() as AddressInfo;
  return { server, url: `http://127.0.0.1:${address.port}`, state, requests };
}

/** mock bridge + deps；用完关。 */
async function withBridge(
  fn: (ctx: { url: string; state: MockBridgeState; requests: RecordedRequest[]; deps: ToolDeps }) => Promise<void>,
): Promise<void> {
  const mock = await startMockBridge();
  try {
    await fn({ url: mock.url, state: mock.state, requests: mock.requests, deps: { bridgeUrl: mock.url, token: 'test-token' } });
  } finally {
    mock.server.close();
  }
}

type Invocable = { invoke: (args: Record<string, unknown>) => Promise<string> };

function asInvocable(record: unknown): Invocable {
  return record as Invocable;
}

const EXPECTED_LEVELS: Record<string, string> = {
  launch_app: 'L1',
  get_weather: 'L0',
  get_location: 'L0',
  speak: 'L1',
  web_fetch: 'L0',
  screen_capture: 'L0',
  screen_understand: 'L0',
  task_start: 'L1',
  task_status: 'L1',
  task_cancel: 'L1',
};

describe('tool registry capabilities metadata', () => {
  it('registers exactly the wave1-b + task capabilities with complete metadata', () => {
    expect(CAPABILITIES.map((c) => c.id).sort()).toEqual(Object.keys(EXPECTED_LEVELS).sort());
    for (const capability of CAPABILITIES) {
      expect(capability.id.trim().length, `${capability.id} id 非空`).toBeGreaterThan(0);
      expect(capability.title.trim().length, `${capability.id} title 非空`).toBeGreaterThan(0);
      expect(capability.description.trim().length, `${capability.id} description 非空`).toBeGreaterThan(0);
      expect(capability.inputSchema, `${capability.id} 有 inputSchema`).toBeDefined();
      expect(EXPECTED_LEVELS[capability.id], `${capability.id} 分级在预期表内`).toBeDefined();
      expect(capability.riskLevel, `${capability.id} 分级一致`).toBe(EXPECTED_LEVELS[capability.id]);
      expect(['instant', 'job']).toContain(capability.latencyClass);
      expect(['none', 'reversible', 'destructive']).toContain(capability.sideEffect);
      expect(typeof capability.mainVisible).toBe('boolean');
      expect(typeof capability.workerVisible).toBe('boolean');
      expect(['local-process', 'http-python', 'builtin-vlm']).toContain(capability.executor.kind);
      if (capability.executor.kind === 'local-process') {
        expect(capability.executor.command.length, `${capability.id} command 非空`).toBeGreaterThan(0);
      }
    }
  });

  it('keeps speak worker-invisible but main-visible, and filtering honours both flags', () => {
    const speak = CAPABILITIES.find((c) => c.id === 'speak');
    expect(speak?.mainVisible).toBe(true);
    expect(speak?.workerVisible).toBe(false);
    const workerTools = listCapabilities({ workerVisible: true }).map((c) => c.id);
    expect(workerTools).not.toContain('speak');
    expect(workerTools).toContain('web_fetch');
    const mainTools = listCapabilities({ mainVisible: true }).map((c) => c.id);
    expect(mainTools).toContain('speak');
    expect(listCapabilities({ ids: ['speak', 'web_fetch'] }).map((c) => c.id)).toEqual(['speak', 'web_fetch']);
    expect(listCapabilities().length).toBe(CAPABILITIES.length);
  });
});

describe('toLangChainTools generation and execution', () => {
  it('produces LangChain tools named by capability id with matching description', async () => {
    await withBridge(async ({ url, deps }) => {
      const tools = toLangChainTools({ mainVisible: true }, deps);
      expect(tools.length).toBe(CAPABILITIES.length);
      for (const record of tools) {
        const t = record as { name: string; description: string };
        const capability = CAPABILITIES.find((c) => c.id === t.name);
        expect(capability, `工具名 ${t.name} 对应注册能力`).toBeDefined();
        expect(t.description).toBe(capability!.description);
      }
    });
  });

  it('fails closed when bridge rejects the token (policy check non-200 → deny)', async () => {
    await withBridge(async ({ state, deps }) => {
      state.status = 403;
      const speakTool = asInvocable(toLangChainTools({ ids: ['speak'] }, deps)[0]);
      const result = JSON.parse(await speakTool.invoke({ text: '你好' })) as {
        ok: boolean;
        error: { code: string };
      };
      expect(result.ok).toBe(false);
      expect(result.error.code).toBe('policy_denied');
      state.status = undefined;
    });
  });

  it('rejects invalid args at the LangChain layer before reaching the tool function', async () => {
    await withBridge(async ({ requests, deps }) => {
      const before = requests.length;
      const speakTool = asInvocable(toLangChainTools({ ids: ['speak'] }, deps)[0]);
      // LangChain 的 DynamicStructuredTool 在 invoke 层校验 schema，坏参数不进工具函数
      await expect(speakTool.invoke({})).rejects.toThrow(/did not match expected schema/);
      expect(requests.length, '没有请求打到 bridge').toBe(before);
      // registry 内部还留有第二层 Zod 校验（直接调函数的防御路径）
      const speak = CAPABILITIES.find((c) => c.id === 'speak');
      expect(speak?.inputSchema.safeParse({}).success).toBe(false);
      expect(speak?.inputSchema.safeParse({ text: '你好' }).success).toBe(true);
    });
  });

  it('returns policy_denied ToolResult when the bridge denies', async () => {
    await withBridge(async ({ state, requests, deps }) => {
      state.check = { outcome: 'deny', reason: 'role policy does not allow capability' };
      const weatherTool = asInvocable(toLangChainTools({ ids: ['get_weather'] }, deps)[0]);
      const result = JSON.parse(await weatherTool.invoke({ city: '上海' })) as {
        ok: boolean;
        error: { code: string; message: string };
      };
      expect(result.ok).toBe(false);
      expect(result.error.code).toBe('policy_denied');
      expect(result.error.message).toContain('role policy');
      const audit = requests.at(-1)?.body.event as Record<string, unknown>;
      expect(audit.outcome).toBe('policy_denied');
      expect(audit.capability_id).toBe('get_weather');
      state.check = { outcome: 'allow', reason: 'mock allow' };
    });
  });

  it('throws PolicyConfirmationError with capability/todo/summary on confirmation_required', async () => {
    await withBridge(async ({ state, deps }) => {
      state.check = {
        outcome: 'confirmation_required',
        reason: 'critical action requires confirmation',
        todo_id: 'todo-42',
        summary: 'terminal：要运行删除命令吗？',
      };
      const weatherTool = asInvocable(toLangChainTools({ ids: ['get_weather'] }, deps)[0]);
      const error: unknown = await weatherTool.invoke({}).then(
        () => undefined,
        (e: unknown) => e,
      );
      expect(isPolicyConfirmationError(error)).toBe(true);
      const confirmation = error as PolicyConfirmationError;
      expect(confirmation.capabilityId).toBe('get_weather');
      expect(confirmation.todoId).toBe('todo-42');
      expect(confirmation.summary).toContain('terminal');
      expect(confirmation.message).toContain('todo-42');
      expect(confirmation instanceof Error).toBe(true);
      state.check = { outcome: 'allow', reason: 'mock allow' };
    });
  });

  it('executes http-python tools end-to-end and reports execution to the audit endpoint', async () => {
    await withBridge(async ({ requests, deps }) => {
      const weatherTool = asInvocable(toLangChainTools({ ids: ['get_weather'] }, deps)[0]);
      const raw = await weatherTool.invoke({ city: '上海' });
      const parsed = toolResultFromDict(JSON.parse(raw));
      expect(parsed.ok).toBe(true);
      expect((parsed.output as Record<string, unknown>).text).toBe('晴，18 度');
      const check = requests.find((r) => r.path === '/internal/policy/check');
      expect(check?.body.capability_id).toBe('get_weather');
      expect(check?.body.session_id).toBe('voice-main');
      expect((check?.body.args as Record<string, unknown>).city).toBe('上海');
      const exec = requests.find((r) => r.path === '/internal/tools/get_weather/exec');
      expect(exec?.body.session_id).toBe('voice-main');
      const audit = requests.filter((r) => r.path === '/internal/audit').at(-1)?.body.event as Record<string, unknown>;
      expect(audit.outcome).toBe('executed');
      expect(audit.capability_id).toBe('get_weather');
      expect(audit.ok).toBe(true);
    });
  });

  it('forwards deps.credential as x-guanjia-credential header and body field on retry', async () => {
    await withBridge(async ({ requests, deps }) => {
      deps.credential = 'cred-3';
      const weatherTool = asInvocable(toLangChainTools({ ids: ['get_weather'] }, deps)[0]);
      await weatherTool.invoke({});
      const exec = requests.find((r) => r.path === '/internal/tools/get_weather/exec')!;
      expect(exec.headers['x-guanjia-credential']).toBe('cred-3');
      expect(exec.body.credential).toBe('cred-3');
      const check = requests.find((r) => r.path === '/internal/policy/check')!;
      expect(check.body.credential).toBe('cred-3');
    });
  });

  it('runs local-process executors without a shell and parses their JSON line', async () => {
    await withBridge(async ({ deps }) => {
      const synthetic: Capability = {
        id: 'unit_echo',
        title: 'Echo',
        description: 'test-only local process capability',
        inputSchema: (await import('zod')).z.object({ label: (await import('zod')).z.string().optional() }),
        latencyClass: 'instant',
        sideEffect: 'none',
        riskLevel: 'L0',
        mainVisible: true,
        workerVisible: true,
        executor: {
          kind: 'local-process',
          command: [
            process.execPath,
            '-e',
            "console.log(JSON.stringify({ok:true, path:'out.jpg', label:process.env.NOPE ?? ''}))",
          ],
          timeoutMs: 10_000,
        },
      };
      const echoTool = asInvocable(buildLangChainTool(synthetic, deps));
      const result = JSON.parse(await echoTool.invoke({ label: 'x' })) as {
        ok: boolean;
        output: { path: string };
      };
      expect(result.ok).toBe(true);
      expect(result.output.path).toBe('out.jpg');
    });
  });

  it('reports local-process tool failures with the script error code', async () => {
    await withBridge(async ({ deps }) => {
      const synthetic: Capability = {
        id: 'unit_fail',
        title: 'Fail',
        description: 'test-only failing local process capability',
        inputSchema: (await import('zod')).z.object({}),
        latencyClass: 'instant',
        sideEffect: 'none',
        riskLevel: 'L0',
        mainVisible: true,
        workerVisible: true,
        executor: {
          kind: 'local-process',
          command: [process.execPath, '-e', "console.log(JSON.stringify({ok:false, error:'not_in_map'}))"],
          timeoutMs: 10_000,
        },
      };
      const failTool = asInvocable(buildLangChainTool(synthetic, deps));
      const result = JSON.parse(await failTool.invoke({})) as { ok: boolean; error: { code: string } };
      expect(result.ok).toBe(false);
      expect(result.error.code).toBe('not_in_map');
    });
  });

  it('screen_understand returns capture_failed for a missing capture_path', async () => {
    await withBridge(async ({ deps }) => {
      const vlmTool = asInvocable(toLangChainTools({ ids: ['screen_understand'] }, deps)[0]);
      const missing = path.join(tmpdir(), `no-such-${Date.now()}.jpg`);
      expect(existsSync(missing)).toBe(false);
      const result = JSON.parse(await vlmTool.invoke({ capture_path: missing, question: '屏幕上有什么' })) as {
        ok: boolean;
        error: { code: string };
      };
      expect(result.ok).toBe(false);
      expect(result.error.code).toBe('capture_failed');
    });
  });
});

// 覆盖临时目录创建/清理的用例（screen_capture 冒烟由 Python 侧与手工冒烟承担）
describe('test scaffolding sanity', () => {
  it('tmpdir is writable', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'registry-test-'));
    expect(existsSync(dir)).toBe(true);
    rmSync(dir, { recursive: true, force: true });
  });
});
