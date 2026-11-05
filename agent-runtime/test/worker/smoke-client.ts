/**
 * 冒烟客户端（Wave1-C 端到端，用后即删）：派真任务并消费 SSE，打印全部事件与 outcome。
 *
 * 任务：用 screen_capture 截图 → screen_understand 看屏幕 → 报告屏幕大致内容。
 * 运行：npx tsx test/worker/smoke-client.ts（需先起 bridge 18801 与 smoke-entry 18800）
 */
import { randomUUID } from 'node:crypto';
import { loadRuntimeConfig } from '../../src/config.js';

const cfg = loadRuntimeConfig();
const base = `http://127.0.0.1:${cfg.port}`;
const headers = { 'Content-Type': 'application/json', authorization: `Bearer ${cfg.token}` };

const body = {
  run_id: randomUUID(),
  task_spec: {
    task_id: `task-smoke-${Date.now()}`,
    revision: 1,
    goal:
      '用 screen_capture 截一张当前屏幕的截图，然后用 screen_understand 看一眼截图内容，' +
      '最后用中文报告屏幕上大致有什么内容（两三句话即可）。',
    constraints: [],
    references: [],
    permission_scope: null,
    priority: 0,
    success_criteria: ['报告必须描述屏幕上的大致内容'],
    delivery_requirements: {},
    resources: [],
  },
  materials: { references: [] },
  memory_packet: { summaries: [] },
  allowed_capabilities: ['screen_capture', 'screen_understand'],
  model_hint: null,
};

const created = await fetch(`${base}/v1/worker/runs`, { method: 'POST', headers, body: JSON.stringify(body) });
if (created.status !== 200) {
  console.error('创建 run 失败：', created.status, await created.text());
  process.exit(1);
}
const { run_id: runId } = (await created.json()) as { run_id: string };
console.log(`run_id: ${runId}（task ${body.task_spec.task_id}）`);

const response = await fetch(`${base}/v1/worker/runs/${runId}/events`, { headers: { authorization: headers.authorization } });
if (response.status !== 200 || response.body === null) {
  console.error('打开事件流失败：', response.status);
  process.exit(1);
}
const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  let idx: number;
  while ((idx = buffer.indexOf('\n\n')) >= 0) {
    const raw = buffer.slice(0, idx);
    buffer = buffer.slice(idx + 2);
    const line = raw.trim();
    if (!line.startsWith('data: ')) continue;
    const payload = line.slice(6);
    if (payload === '[DONE]') continue;
    const { seq, event } = JSON.parse(payload) as { seq: number; event: Record<string, unknown> };
    const summary =
      event.type === 'tool_trace'
        ? `tool_trace ${event.capability_id} ok=${event.ok} ${String(event.summary ?? '').slice(0, 160)}`
        : event.type === 'progress'
          ? `progress ${event.stage} ${event.percent ?? '-'}% ${String(event.message ?? '')}`
          : event.type === 'approval_request'
            ? `approval_request ${event.capability}(${event.request_id}) ${String(event.rationale ?? '').slice(0, 120)}`
            : `outcome ${event.status}`;
    console.log(`[seq ${seq}] ${summary}`);
    if (event.type === 'outcome') {
      console.log('----- outcome -----');
      console.log(JSON.stringify(event, null, 2).slice(0, 4000));
      process.exit(0);
    }
  }
}
console.error('事件流关闭但未收到 outcome');
process.exit(1);
