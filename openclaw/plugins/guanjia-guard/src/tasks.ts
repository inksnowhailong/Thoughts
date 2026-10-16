import fs from "node:fs";
import path from "node:path";

import { DIRECT_ONLY } from "./ps-tools.ts";

const TASK_ID = /^[0-9a-f]{32}$/;

type FetchLike = (url: string, init: { method?: string; headers?: Record<string, string>; body?: string; signal: AbortSignal }) => Promise<{
  status: number; json(): Promise<any>;
}>;

interface TaskToolOptions {
  workspaceDir: string;
  fetchImpl?: FetchLike;
}

function textResult(text: string, details: Record<string, unknown>) {
  return { content: [{ type: "text", text }], details };
}

function descriptor(workspaceDir: string): { port: number; capability: string } | undefined {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(workspaceDir, "guanjia", "task-ipc.json"), "utf8"));
    return Number.isInteger(value.port) && typeof value.capability === "string" ? value : undefined;
  } catch {
    return undefined;
  }
}

function client(opts: TaskToolOptions) {
  const doFetch = opts.fetchImpl ?? globalThis.fetch as unknown as FetchLike;
  return async (method: "GET" | "POST", route: string, body?: object) => {
    const cfg = descriptor(opts.workspaceDir);
    if (!cfg) return { ok: false, error: "unavailable" };
    try {
      const response = await doFetch(`http://127.0.0.1:${cfg.port}${route}`, {
        method,
        headers: { "X-Guanjia-Task-Cap": cfg.capability, ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: AbortSignal.timeout(1000),
      });
      return response.status === 200 ? await response.json() : { ok: false, error: "rejected" };
    } catch {
      return { ok: false, error: "unavailable" };
    }
  };
}

/** 守卫内部调用：仅把 pending 的 id/摘要绑回对应任务，永不传原始工具参数。 */
export async function bindTaskApproval(workspaceDir: string, input: {
  sessionKey?: string; approvalId: string; description: string; expiresAt: number;
}, fetchImpl?: FetchLike) {
  const match = /^agent:main:task-([0-9a-f]{32})$/.exec(input.sessionKey ?? "");
  if (!match) return false;
  const result = await client({ workspaceDir, fetchImpl })("POST", "/approval", {
    id: match[1], approval_id: input.approvalId, description: input.description, expires_at: input.expiresAt,
  });
  return result.ok === true;
}

/** 守卫审批决定后通知语音任务执行器；只传 id 和决定，不含原始参数。 */
export async function decideTaskApproval(workspaceDir: string, input: {
  sessionKey?: string; decision: "approve" | "reject";
}) {
  const match = /^agent:main:task-([0-9a-f]{32})$/.exec(input.sessionKey ?? "");
  if (!match) return false;
  const result = await client({ workspaceDir })("POST", "/approval-decision", { id: match[1], decision: input.decision });
  return result.ok === true;
}

function unavailable() {
  return textResult("任务管家暂时不可用，没有创建或执行任何任务。", { ok: false, error: "unavailable" });
}

export function createTaskStartTool(opts: TaskToolOptions) {
  const call = client(opts);
  return {
    name: "task_start", label: "Start background task", catalogMode: DIRECT_ONLY,
    description: "Create a persistent background task for a multi-step task likely over 5 seconds. It only queues a task and never executes commands, downloads, or a model.",
    parameters: { type: "object", properties: { summary: { type: "string" }, goal: { type: "string" } }, required: ["summary", "goal"], additionalProperties: false },
    async execute(_id: string, params: { summary?: unknown; goal?: unknown }) {
      if (typeof params.summary !== "string" || typeof params.goal !== "string") return textResult("任务没有创建：参数不对。", { ok: false, error: "bad_input" });
      const result = await call("POST", "/start", { summary: params.summary, goal: params.goal });
      return result.ok ? textResult(`任务已创建：${result.task.summary}。`, { ok: true, task: result.task }) : unavailable();
    },
  };
}

export function createTaskStatusTool(opts: TaskToolOptions) {
  const call = client(opts);
  return {
    name: "task_status", label: "Get task status", catalogMode: DIRECT_ONLY,
    description: "Read task summary or one task status. Does not reveal task goals, worker session keys, or transcripts.",
    parameters: { type: "object", properties: { id: { type: "string" } }, additionalProperties: false },
    async execute(_id: string, params: { id?: unknown }) {
      if (params.id !== undefined && (typeof params.id !== "string" || !TASK_ID.test(params.id))) return textResult("任务编号不对。", { ok: false, error: "bad_id" });
      const result = await call("GET", `/status${params.id ? `?id=${encodeURIComponent(params.id)}` : ""}`);
      return result.ok ? textResult(params.id ? `任务状态：${result.task.status}。` : "已读取任务摘要。", result.task ?? result.summary) : unavailable();
    },
  };
}

export function createTaskCancelTool(opts: TaskToolOptions) {
  const call = client(opts);
  return {
    name: "task_cancel", label: "Request task cancellation", catalogMode: DIRECT_ONLY,
    description: "Record a cancellation request for a task. It never kills external processes.",
    parameters: { type: "object", properties: { id: { type: "string" }, reason: { type: "string" } }, required: ["id"], additionalProperties: false },
    async execute(_id: string, params: { id?: unknown; reason?: unknown }) {
      if (typeof params.id !== "string" || !TASK_ID.test(params.id) || (params.reason !== undefined && typeof params.reason !== "string")) {
        return textResult("任务编号或原因不对。", { ok: false, error: "bad_input" });
      }
      const result = await call("POST", "/cancel", { id: params.id, reason: params.reason ?? "" });
      return result.ok ? textResult(`已请求取消任务：${result.task.summary}。`, { ok: true, task: result.task }) : unavailable();
    },
  };
}

export function createTaskReportTool(opts: TaskToolOptions) {
  const call = client(opts);
  return {
    name: "task_report", label: "Report task progress", catalogMode: DIRECT_ONLY,
    description: "Report concise progress for a running task. This only records task state; it never performs work.",
    parameters: { type: "object", properties: { id: { type: "string" }, progress: { type: "string" }, percent: { type: "number" } }, required: ["id", "progress"], additionalProperties: false },
    async execute(_id: string, params: { id?: unknown; progress?: unknown; percent?: unknown }) {
      if (typeof params.id !== "string" || !TASK_ID.test(params.id) || typeof params.progress !== "string" ||
          (params.percent !== undefined && typeof params.percent !== "number")) return textResult("任务进度参数不对。", { ok: false, error: "bad_input" });
      const result = await call("POST", "/report", { id: params.id, progress: params.progress, percent: params.percent });
      return result.ok ? textResult(`已记录任务进度：${result.task.progress || "已更新"}。`, { ok: true, task: result.task }) : unavailable();
    },
  };
}
