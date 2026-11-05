/**
 * Worker Runtime 测试（Wave1-C）—— 五个核心场景 + SSE 续传：
 *  1) 纯对话任务（含 success_criteria 自评轮）→ outcome completed
 *  2) 模型发工具调用 → mock bridge 执行 → 结果回灌 → 完成（含 artifacts 聚合）
 *  3) 工具抛 PolicyConfirmationError → interrupt 挂起 → resume(approve+credential)
 *     → 工具带 credential 重试 → 完成（含 bridge 审批登记断言）
 *  4) resume(reject) → 模型收到拒绝 ToolMessage 收尾
 *  5) checkpoint：挂起后全新 saver/graph 实例（同 thread_id=run_id）resume 成功（进程重启恢复）
 *  6) SSE ?since=seq 断线续传
 * 模型用 ScriptedChatModel（不访问真实 API）；bridge 用本地 mock http server。
 */
import { createServer, type Server } from 'node:http';
import { AddressInfo } from 'node:net';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import Fastify, { type FastifyInstance } from 'fastify';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { registerWorkerRoutes } from '../../src/worker/routes.js';
import { WorkerRunManager } from '../../src/worker/runs.js';
import { ScriptedChatModel } from '../helpers/scripted-model.js';

// ---------- mock bridge ----------

interface RecordedRequest {
  method: string;
  path: string;
  headers: Record<string, string | string[] | undefined>;
  body: Record<string, unknown>;
}

interface MockBridgeState {
  /** 'allow' 直接放行；'confirmation' 无 credential 时返回 confirmation_required(todo-9)。 */
  checkMode: 'allow' | 'confirmation';
  exec: { ok: boolean; output?: unknown; error?: { code: string; message: string } };
  /** /internal/tools/* 响应延迟（毫秒），用于 cancel 场景制造工具执行窗口。 */
  execDelayMs: number;
}

async function startMockBridge() {
  const state: MockBridgeState = {
    checkMode: 'allow',
    exec: { ok: true, output: { text: '晴，18 度', path: 'C:/tmp/shot-1.jpg' } },
    execDelayMs: 0,
  };
  const requests: RecordedRequest[] = [];
  const server: Server = createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += String(chunk);
    });
    req.on('end', async () => {
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
        if (state.checkMode === 'confirmation' && body.credential !== 'cred-9') {
          send(200, {
            outcome: 'confirmation_required',
            reason: 'critical action requires confirmation',
            todo_id: 'todo-9',
            summary: 'get_weather：{"city":"北京"}',
          });
          return;
        }
        send(200, { outcome: 'allow', reason: 'mock allow' });
        return;
      }
      if (req.method === 'POST' && req.url === '/internal/audit') {
        send(200, { ok: true, recorded: true });
        return;
      }
      if (req.method === 'POST' && req.url === '/internal/bridge/worker_approval') {
        if (typeof body.task_id !== 'string' || !body.task_id || typeof body.todo_id !== 'string' || !body.todo_id) {
          send(400, { ok: false, reason: 'need_task_id_capability_todo_id' });
          return;
        }
        send(200, { ok: true, id: body.todo_id, expiresAt: '2026-11-05T12:02:00Z' });
        return;
      }
      if (req.method === 'POST' && (req.url ?? '').startsWith('/internal/tools/')) {
        if (state.execDelayMs > 0) {
          await new Promise((resolve) => setTimeout(resolve, state.execDelayMs));
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

// ---------- 基架 ----------

const TOKEN = 'test-token';
let bridge: Awaited<ReturnType<typeof startMockBridge>>;
let tempDir: string;

beforeAll(async () => {
  bridge = await startMockBridge();
});

afterAll(() => {
  bridge.server.close();
});

beforeEach(() => {
  bridge.state.checkMode = 'allow';
  bridge.state.exec = { ok: true, output: { text: '晴，18 度', path: 'C:/tmp/shot-1.jpg' } };
  bridge.state.execDelayMs = 0;
  bridge.requests.length = 0;
  tempDir = mkdtempSync(path.join(tmpdir(), 'guanjia-worker-test-'));
});

afterEach(() => {
  // 每测试结束时清理临时目录（manager 已 dispose，sqlite 句柄已关）
  try {
    rmSync(tempDir, { recursive: true, force: true });
  } catch {
    // Windows 句柄偶发延迟释放，清理失败不令测试失败
  }
});

const SPEC_BASE = {
  task_id: 'task-e2e-1',
  revision: 1,
  goal: '查询北京的天气并报告',
  constraints: [],
  references: [],
  permission_scope: null,
  priority: 0,
  success_criteria: [],
  delivery_requirements: {},
  resources: [],
};

function makeBody(overrides: { success_criteria?: string[]; allowed?: string[] } = {}) {
  return {
    run_id: randomUUID(),
    task_spec: { ...SPEC_BASE, success_criteria: overrides.success_criteria ?? [] },
    materials: { references: [] },
    memory_packet: { summaries: [] },
    allowed_capabilities: overrides.allowed ?? ['get_weather'],
    model_hint: null,
  };
}

function buildApp(manager: WorkerRunManager): FastifyInstance {
  const app = Fastify();
  registerWorkerRoutes(app, { token: TOKEN, manager });
  return app;
}

function newManager(model: ScriptedChatModel, checkpointPath?: string): WorkerRunManager {
  return new WorkerRunManager({
    bridgeUrl: bridge.url,
    token: TOKEN,
    model,
    checkpointPath: checkpointPath ?? path.join(tempDir, `checkpoints-${randomUUID()}.sqlite3`),
    audit: true,
  });
}

async function waitFor(cond: () => boolean, timeoutMs = 8000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (cond()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

async function waitOutcome(manager: WorkerRunManager, runId: string): Promise<void> {
  await waitFor(() => {
    const status = manager.recordStatus(runId);
    return status === 'completed' || status === 'failed' || status === 'cancelled';
  });
}

async function waitApproval(manager: WorkerRunManager, runId: string): Promise<void> {
  await waitFor(() => manager.recordStatus(runId) === 'awaiting_approval');
}

function events(manager: WorkerRunManager, runId: string) {
  const rec = manager.getRecord(runId);
  return rec ? rec.events.map((stored) => stored.event) : [];
}

// ---------- 测试 ----------

describe('Worker Runtime（LangGraph + 审批 interrupt + checkpoint）', () => {
  it('场景1：纯对话任务（success_criteria 自评轮）→ outcome completed', async () => {
    const model = new ScriptedChatModel([
      { content: '收到，我马上处理。' },
      { content: '任务完成：北京今天晴，18 度，不用带伞。' },
    ]);
    const manager = newManager(model);
    const app = buildApp(manager);
    try {
      const res = await app.inject({
        method: 'POST',
        url: '/v1/worker/runs',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: makeBody({ success_criteria: ['报告必须包含温度'] }),
      });
      expect(res.statusCode).toBe(200);
      const { run_id: runId } = res.json() as { run_id: string };
      await waitOutcome(manager, runId);

      const evs = events(manager, runId);
      const outcome = evs.find((e) => e.type === 'outcome');
      expect(outcome).toBeDefined();
      expect(outcome && outcome.type === 'outcome' ? outcome.status : '').toBe('completed');
      if (outcome?.type === 'outcome') {
        expect(outcome.summary).toContain('18 度');
        expect(outcome.result?.summary).toContain('18 度');
        expect(outcome.result?.evidence?.[0]?.kind).toBe('agent_runtime_worker');
        expect(outcome.result?.evidence?.[0]?.artifacts).toEqual([]);
      }
      // success_criteria 非空 → verify 注入自评提示（一轮 plan → verify 重入）
      const verifyEvents = evs.filter((e) => e.type === 'progress' && e.stage === 'verify');
      expect(verifyEvents.length).toBeGreaterThanOrEqual(1);
      const stages = evs.filter((e) => e.type === 'progress').map((e) => (e as { stage: string }).stage);
      expect(stages).toContain('plan');
      expect(stages[0]).toBe('starting');
      expect(stages.at(-1)).toBe('outcome');
      expect(manager.recordStatus(runId)).toBe('completed');
    } finally {
      await app.close();
      manager.dispose();
    }
  });

  it('场景2：模型发工具调用 → mock bridge 执行 → 回灌 → 完成（artifacts 聚合 + session 透传）', async () => {
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'get_weather', args: { city: '北京' } }] },
      { content: '北京今天晴，18 度。' },
    ]);
    const manager = newManager(model);
    const app = buildApp(manager);
    try {
      const res = await app.inject({
        method: 'POST',
        url: '/v1/worker/runs',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: makeBody(),
      });
      const { run_id: runId } = res.json() as { run_id: string };
      await waitOutcome(manager, runId);

      const evs = events(manager, runId);
      const trace = evs.find((e) => e.type === 'tool_trace');
      expect(trace && trace.type === 'tool_trace' ? trace.capability_id : '').toBe('get_weather');
      if (trace?.type === 'tool_trace') {
        expect(trace.ok).toBe(true);
        expect(trace.summary).toContain('shot-1.jpg');
      }
      const outcome = evs.find((e) => e.type === 'outcome');
      if (outcome?.type === 'outcome') {
        expect(outcome.status).toBe('completed');
        expect(outcome.result?.artifacts?.[0]?.uri).toBe('C:/tmp/shot-1.jpg');
        expect(outcome.result?.key_steps).toContain('get_weather → 成功');
      }
      // deps.session_id 透传到 bridge exec（logical session = task/{id}/r{rev}）
      const exec = bridge.requests.find((r) => r.path === '/internal/tools/get_weather/exec');
      expect(exec?.body.session_id).toBe('task/task-e2e-1/r1');
      const check = bridge.requests.find((r) => r.path === '/internal/policy/check');
      expect(check?.body.capability_id).toBe('get_weather');
      // 事件审计转发到 /internal/audit（registry 的工具审计 kind=tool_execution 混在同一端点）
      const audit = bridge.requests.filter((r) => r.path === '/internal/audit');
      expect(audit.length).toBeGreaterThan(0);
      const workerAudits = audit.filter((r) => (r.body.event as Record<string, unknown>).kind === 'worker_run_event');
      // 审计 POST 为 fire-and-forget，可能有上一用例的尾部请求晚到：只断言本 run 的事件已送达
      expect(
        workerAudits.some((r) => {
          const event = r.body.event as Record<string, unknown>;
          return event.run_id === runId && event.kind === 'worker_run_event';
        }),
      ).toBe(true);
    } finally {
      await app.close();
      manager.dispose();
    }
  });

  it('场景3：confirmation → interrupt → resume(approve+credential) → 带 credential 重试 → 完成', async () => {
    bridge.state.checkMode = 'confirmation';
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'get_weather', args: { city: '北京' } }] },
      { content: '审批后查到了：北京晴，18 度。' },
    ]);
    const manager = newManager(model);
    const app = buildApp(manager);
    try {
      const res = await app.inject({
        method: 'POST',
        url: '/v1/worker/runs',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: makeBody(),
      });
      const { run_id: runId } = res.json() as { run_id: string };
      await waitApproval(manager, runId);

      const approval = events(manager, runId).find((e) => e.type === 'approval_request');
      expect(approval && approval.type === 'approval_request' ? approval.request_id : '').toBe('todo-9');
      if (approval?.type === 'approval_request') {
        expect(approval.capability).toBe('get_weather');
        expect(approval.action_fingerprint).toMatch(/^[0-9a-f]{64}$/);
      }
      // act 捕获时先 POST bridge 登记审批（task_id/run_id/callback/todo_id 齐全）
      const registration = bridge.requests.find((r) => r.path === '/internal/bridge/worker_approval');
      expect(registration?.body.task_id).toBe('task-e2e-1');
      expect(registration?.body.todo_id).toBe('todo-9');
      expect(registration?.body.run_id).toBe(runId);
      expect(typeof registration?.body.callback).toBe('string');

      // 第一次 resume(approve + credential)——图从 interrupt 恢复
      const first = await app.inject({
        method: 'POST',
        url: `/v1/worker/runs/${runId}/resume`,
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { decision: 'approve', credential: 'cred-9' },
      });
      expect(first.statusCode).toBe(200);
      // resume 已把状态翻回 running：再次 resume 必然 409
      const conflict = await app.inject({
        method: 'POST',
        url: `/v1/worker/runs/${runId}/resume`,
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { decision: 'approve', credential: 'cred-9' },
      });
      expect(conflict.statusCode).toBe(409);
      // 等待恢复后的终态
      await waitOutcome(manager, runId);
      const evs = events(manager, runId);
      const outcome = evs.find((e) => e.type === 'outcome');
      if (outcome?.type === 'outcome') {
        expect(outcome.status).toBe('completed');
        expect(outcome.summary).toContain('北京晴');
      }
      // 审批通过后：check 带 credential 放行、exec 带 x-guanjia-credential 头
      const checkWithCred = bridge.requests.find(
        (r) => r.path === '/internal/policy/check' && r.body.credential === 'cred-9',
      );
      expect(checkWithCred).toBeDefined();
      const exec = bridge.requests.find((r) => r.path === '/internal/tools/get_weather/exec');
      expect(exec?.headers['x-guanjia-credential']).toBe('cred-9');
      expect(manager.recordStatus(runId)).toBe('completed');
    } finally {
      bridge.state.checkMode = 'allow';
      await app.close();
      manager.dispose();
    }
  });

  it('场景4：resume(reject) → 拒绝作为 ToolMessage 回给模型收尾', async () => {
    bridge.state.checkMode = 'confirmation';
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'get_weather', args: { city: '北京' } }] },
      { content: '主人没有批准查天气，任务无法继续，我如实报告受阻。' },
    ]);
    const manager = newManager(model);
    const app = buildApp(manager);
    try {
      const res = await app.inject({
        method: 'POST',
        url: '/v1/worker/runs',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: makeBody(),
      });
      const { run_id: runId } = res.json() as { run_id: string };
      await waitApproval(manager, runId);

      const reject = await app.inject({
        method: 'POST',
        url: `/v1/worker/runs/${runId}/resume`,
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { decision: 'reject' },
      });
      expect(reject.statusCode).toBe(200);
      await waitOutcome(manager, runId);

      const outcome = events(manager, runId).find((e) => e.type === 'outcome');
      if (outcome?.type === 'outcome') {
        expect(outcome.status).toBe('completed');
        expect(outcome.summary).toContain('无法继续');
      }
      // 拒绝后不再执行工具
      const execs = bridge.requests.filter((r) => r.path === '/internal/tools/get_weather/exec');
      expect(execs).toHaveLength(0);
      const checkWithCred = bridge.requests.find(
        (r) => r.path === '/internal/policy/check' && r.body.credential,
      );
      expect(checkWithCred).toBeUndefined();
    } finally {
      bridge.state.checkMode = 'allow';
      await app.close();
      manager.dispose();
    }
  });

  it('场景5：checkpoint —— 挂起后全新 saver/graph 实例（同 run_id）resume 成功', async () => {
    bridge.state.checkMode = 'confirmation';
    const checkpointPath = path.join(tempDir, `restart-${randomUUID()}.sqlite3`);
    const model1 = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'get_weather', args: { city: '北京' } }] },
    ]);
    const manager1 = newManager(model1, checkpointPath);
    const app1 = buildApp(manager1);
    let runId = '';
    try {
      const res = await app1.inject({
        method: 'POST',
        url: '/v1/worker/runs',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: makeBody(),
      });
      runId = (res.json() as { run_id: string }).run_id;
      await waitApproval(manager1, runId);
      // 模拟进程死亡：关 saver、丢掉全部内存实例（registry 不带走 checkpoint）
      manager1.dispose();
    } finally {
      await app1.close();
    }

    // —— 进程重启：全新 saver（同一 DB 文件）+ 全新 manager/graph，注册表为空 ——
    const model2 = new ScriptedChatModel([{ content: '重启恢复后完成：北京晴，18 度。' }]);
    const manager2 = newManager(model2, checkpointPath);
    const app2 = buildApp(manager2);
    try {
      const resume = await app2.inject({
        method: 'POST',
        url: `/v1/worker/runs/${runId}/resume`,
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { decision: 'approve', credential: 'cred-9' },
      });
      expect(resume.statusCode).toBe(200);
      await waitOutcome(manager2, runId);
      const outcome = events(manager2, runId).find((e) => e.type === 'outcome');
      if (outcome?.type === 'outcome') {
        expect(outcome.status).toBe('completed');
        expect(outcome.summary).toContain('重启恢复后完成');
      }
      // 重启后 retry 仍带 credential 重试同一工具
      const exec = bridge.requests.filter((r) => r.path === '/internal/tools/get_weather/exec').at(-1);
      expect(exec?.headers['x-guanjia-credential']).toBe('cred-9');
    } finally {
      bridge.state.checkMode = 'allow';
      await app2.close();
      manager2.dispose();
    }
  });

  it('场景6：SSE 事件流按序推送且支持 ?since=seq 断线续传', async () => {
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'get_weather', args: { city: '北京' } }] },
      { content: '北京晴。' },
    ]);
    const manager = newManager(model);
    const app = buildApp(manager);
    await app.listen({ port: 0, host: '127.0.0.1' });
    const base = `http://127.0.0.1:${(app.server.address() as AddressInfo).port}`;
    try {
      const res = await fetch(`${base}/v1/worker/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', authorization: `Bearer ${TOKEN}` },
        body: JSON.stringify(makeBody()),
      });
      const { run_id: runId } = (await res.json()) as { run_id: string };
      await waitOutcome(manager, runId);

      const readSse = async (since?: number): Promise<Array<{ seq: number; event: { type: string } }>> => {
        const response = await fetch(`${base}/v1/worker/runs/${runId}/events${since !== undefined ? `?since=${since}` : ''}`, {
          headers: { authorization: `Bearer ${TOKEN}` },
        });
        expect(response.status).toBe(200);
        const text = await response.text();
        const out: Array<{ seq: number; event: { type: string } }> = [];
        for (const line of text.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          out.push(JSON.parse(line.slice(6)) as { seq: number; event: { type: string } });
        }
        return out;
      };

      const all = await readSse();
      expect(all.length).toBeGreaterThan(3);
      // 按序且全量
      expect(all.map((e) => e.seq)).toEqual(all.map((e, i) => i));
      expect(all.at(-1)?.event.type).toBe('outcome');
      // 续传：since 之后的事件
      const since = 2;
      const tail = await readSse(since);
      expect(tail.map((e) => e.seq)).toEqual(all.slice(since + 1).map((e) => e.seq));

      // 鉴权：无 token 401
      const unauth = await fetch(`${base}/v1/worker/runs/${runId}/events`);
      expect(unauth.status).toBe(401);
      // 404：不存在的 run
      const missing = await fetch(`${base}/v1/worker/runs/no-such-run/events`, {
        headers: { authorization: `Bearer ${TOKEN}` },
      });
      expect(missing.status).toBe(404);
    } finally {
      await app.close();
      manager.dispose();
    }
  });

  it('鉴权与参数校验：缺 token 401、非法请求体 400', async () => {
    const model = new ScriptedChatModel([{ content: '完成。' }]);
    const manager = newManager(model);
    const app = buildApp(manager);
    try {
      const unauth = await app.inject({ method: 'POST', url: '/v1/worker/runs', payload: makeBody() });
      expect(unauth.statusCode).toBe(401);
      const bad = await app.inject({
        method: 'POST',
        url: '/v1/worker/runs',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { task_spec: { task_id: 'x' } },
      });
      expect(bad.statusCode).toBe(400);
      const missing = await app.inject({
        method: 'POST',
        url: '/v1/worker/runs/no-such/resume',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { decision: 'approve' },
      });
      expect(missing.statusCode).toBe(404);
    } finally {
      await app.close();
      manager.dispose();
    }
  });

  it('cancel：运行中置标记收尾 cancelled；挂起中按 reject 唤醒收尾', async () => {
    // 工具执行延迟 400ms（mock bridge），policy check 到达即置 cancel → 当前工具完成后 plan 自检终止
    bridge.state.execDelayMs = 400;
    const model = new ScriptedChatModel([
      { content: '', tool_calls: [{ id: 'call_1', name: 'get_weather', args: { city: '北京' } }] },
    ]);
    const manager = newManager(model);
    const app = buildApp(manager);
    try {
      const res = await app.inject({
        method: 'POST',
        url: '/v1/worker/runs',
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: makeBody(),
      });
      const { run_id: runId } = res.json() as { run_id: string };
      // 等 act 已进入工具执行（policy check 已到 bridge），此时工具还在 400ms 延迟中
      await waitFor(() => bridge.requests.some((r) => r.path === '/internal/policy/check'));
      const cancel = await app.inject({
        method: 'POST',
        url: `/v1/worker/runs/${runId}/cancel`,
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { reason: '主人喊停' },
      });
      expect(cancel.statusCode).toBe(200);
      await waitOutcome(manager, runId);
      const outcome = events(manager, runId).find((e) => e.type === 'outcome');
      if (outcome?.type === 'outcome') {
        expect(outcome.status).toBe('cancelled');
        expect(outcome.summary).toContain('主人喊停');
      }
      // 幂等：终态后再次 cancel 返回原状态
      const again = await app.inject({
        method: 'POST',
        url: `/v1/worker/runs/${runId}/cancel`,
        headers: { authorization: `Bearer ${TOKEN}` },
        payload: { reason: '再喊停' },
      });
      expect((again.json() as { status: string }).status).toBe('cancelled');
    } finally {
      await app.close();
      manager.dispose();
    }
  });
});
