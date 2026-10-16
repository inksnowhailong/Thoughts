import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { bindTaskApproval, createTaskCancelTool, createTaskStartTool } from "./tasks.ts";

const check = (value: unknown, name: string) => {
  if (!value) throw new Error(`FAIL ${name}`);
  console.log(`PASS ${name}`);
};

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "guanjia-task-tool-"));
fs.mkdirSync(path.join(dir, "guanjia"));
fs.writeFileSync(path.join(dir, "guanjia", "task-ipc.json"), JSON.stringify({ port: 19999, capability: "cap" }));
let request: any;
const fetchImpl: any = async (url: string, init: any) => {
  request = { url, init };
  return { status: 200, json: async () => ({ ok: true, task: { id: "a".repeat(32), summary: "整理资料", status: "queued" } }) };
};

const start = createTaskStartTool({ workspaceDir: dir, fetchImpl });
const created = await start.execute("id", { summary: "整理资料", goal: "仅创建，不执行" });
check(created.details.ok === true && /已创建/.test(created.content[0].text), "task_start returns queued summary");
check(request.url.endsWith("/start") && request.init.headers["X-Guanjia-Task-Cap"] === "cap", "task tools use capability IPC");
check(!created.details.task.goal && !created.details.task.session_key, "tool response keeps goal and session private");
check(await bindTaskApproval(dir, {
  sessionKey: `agent:main:task-${"b".repeat(32)}`, approvalId: "pending-1", description: "删除文件", expiresAt: 1,
}, fetchImpl), "guard can bind a task-session approval without parameters");
check(request.url.endsWith("/approval") && !request.init.body.includes("command"), "approval binding sends only id and summary");

const cancel = createTaskCancelTool({ workspaceDir: dir, fetchImpl });
const bad = await cancel.execute("id", { id: "bad" });
check(bad.details.error === "bad_input", "task_cancel rejects invalid ids before IPC");
const unavailable = createTaskStartTool({ workspaceDir: path.join(dir, "missing"), fetchImpl });
const absent = await unavailable.execute("id", { summary: "x", goal: "y" });
check(absent.details.error === "unavailable", "missing descriptor fails closed");
fs.rmSync(dir, { recursive: true, force: true });
console.log("ALL PASS tasks.test");
