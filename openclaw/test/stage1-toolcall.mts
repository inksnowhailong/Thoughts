// 阶段 1 tool_call 定级用例：外层按真实工具定级、内层去重、client 来源强制 L3、L3 超时 120 秒。
// 运行：cd F:\coding\guanjia\openclaw && npx --yes tsx test\stage1-toolcall.mts
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { resolveToolCallTarget, type LevelsFile } from "../plugins/guanjia-guard/src/levels";
import { createGuard, parentToolCallIdOf, sanitizeToolCallIdPart, L3_APPROVAL_TIMEOUT_MS } from "../plugins/guanjia-guard/src/guard";
import { createStore } from "../plugins/guanjia-guard/src/store";

const pluginDir = path.resolve(import.meta.dirname, "../plugins/guanjia-guard");
const levels = JSON.parse(fs.readFileSync(path.join(pluginDir, "defaults/levels.json"), "utf8")) as LevelsFile;
const store = createStore(fs.mkdtempSync(path.join(os.tmpdir(), "gj-s1-")), path.join(pluginDir, "defaults"));
const g = createGuard(store, levels);
const lastAudit = () => JSON.parse(fs.readFileSync(store.file("audit.jsonl"), "utf8").trim().split("\n").at(-1)!);
const outer = (toolCallId: string, id: string, args: Record<string, unknown>, sessionKey = "s1") =>
  g.beforeToolCall({ toolName: "tool_call", params: { id, args }, toolCallId }, { sessionKey, runId: "r1" }) as any;
const inner = (toolCallId: string, toolName: string, params: Record<string, unknown>, ctx: any = undefined) =>
  g.beforeToolCall({ toolName, params, toolCallId }, ctx) as any;

// ---------- 1. 还原 tool_call 目标 ----------
assert.deepEqual(resolveToolCallTarget({ id: "openclaw:core:automations", args: { action: "add" } }), { toolName: "automations", source: "openclaw", params: { action: "add" } });
assert.deepEqual(resolveToolCallTarget({ id: "exec", args: { command: "Start-Process notepad" } }), { toolName: "exec", source: undefined, params: { command: "Start-Process notepad" } });
assert.deepEqual(resolveToolCallTarget({ id: "client:client:open_door", args: {} }), { toolName: "open_door", source: "client", params: {} });
assert.deepEqual(resolveToolCallTarget({ id: "mcp:fs:read_file", "args.path": "a.txt" }), { toolName: "read_file", source: "mcp", params: { path: "a.txt" } });
assert.deepEqual(resolveToolCallTarget({ id: "openclaw:core:bash", command: "dir" }), { toolName: "exec", source: "openclaw", params: { command: "dir" } });
assert.equal(resolveToolCallTarget({ args: { command: "dir" } }), undefined);
assert.equal(parentToolCallIdOf("tool_search_code:call_1:exec:3"), "call_1");
assert.equal(parentToolCallIdOf("tool_search_code:a:b:exec:12"), "a:b");
assert.equal(parentToolCallIdOf("call_1"), undefined);
assert.equal(sanitizeToolCallIdPart("call/abc 1"), "call_abc_1");

// ---------- 2. 外层按真实工具定级 ----------
assert.equal(outer("c_np", "openclaw:core:exec", { command: "Start-Process notepad" }), undefined, "打开记事本不再撞默认 L2");
let a = lastAudit();
assert.equal(a.tool, "exec");
assert.equal(a.level, "L1");
assert.equal(a.via, "tool_call");
assert.equal(a.catalogSource, "openclaw");
assert.equal(a.decision, "allow");
assert.equal(outer("c_poll", "openclaw:core:process", { action: "poll", sessionId: "x" }), undefined, "process poll 经 tool_call 也是 L0");
assert.equal(outer("c_web", "openclaw:core:web_fetch", { url: "https://wttr.in/?format=3" }), undefined, "web_fetch 经 tool_call 放行");
const noId = g.beforeToolCall({ toolName: "tool_call", params: { args: {} }, toolCallId: "c_noid" }, { sessionKey: "s0" }) as any;
assert.equal(noId.block, true, "解析不出目标时按 tool_call 本身定级（默认 L2）");

// ---------- 3. 内层去重：同一次调用只确认一次 ----------
// L2 经 tool_call：外层确认一次；内层的 ctx 没有 sessionKey，不去重就会被判"无人确认"拦下
const w = { path: "D:\\x.txt", content: "hi" };
assert.equal(outer("c_w1", "openclaw:core:write", w, "s2").block, true);
assert.equal(lastAudit().decision, "block-need-confirm");
assert.ok(g.onUserTurn("s2", "确认"));
assert.equal(outer("c_w2", "openclaw:core:write", w, "s2"), undefined, "确认后外层同参放行");
assert.equal(lastAudit().decision, "allow-confirmed");
assert.equal(inner("tool_search_code:c_w2:write:1", "write", w), undefined, "内层凭外层登记放行，不要求第二次确认");
assert.equal(lastAudit().decision, "allow-inner-dedup");
assert.equal(inner("tool_search_code:c_w2:write:2", "write", w).block, true, "登记只能用一次");

// L3 经 tool_call：批准一次后内层直接放行
const off = { command: "shutdown /s /t 1800" };
const r3 = outer("c_s1", "openclaw:core:exec", off);
assert.ok(r3.requireApproval, "关机走带外审批");
assert.equal(r3.requireApproval.timeoutMs, 120_000);
assert.equal(L3_APPROVAL_TIMEOUT_MS, 120_000);
assert.match(r3.requireApproval.title, /exec/);
r3.requireApproval.onResolution("allow-once");
assert.equal(inner("tool_search_code:c_s1:exec:1", "exec", off), undefined, "批准后内层不再第二次审批");
// 被拒绝的 L3 不留登记
const r4 = outer("c_s2", "openclaw:core:exec", off);
r4.requireApproval.onResolution("deny");
assert.ok(inner("tool_search_code:c_s2:exec:1", "exec", off, { sessionKey: "s1" }).requireApproval, "拒绝后没有放行登记");
// 没有外层登记的内层（如 tool_search_code 代码模式）按真实工具正常定级
assert.ok(inner("tool_search_code:code_9:exec:1", "exec", off, { sessionKey: "s1" }).requireApproval);
// 工具名对不上不去重
assert.equal(outer("c_m", "openclaw:core:exec", { command: "Start-Process notepad" }), undefined);
assert.equal(inner("tool_search_code:c_m:write:1", "write", w, { sessionKey: "s3" }).block, true);
// 外层 id 带特殊字符：按 OpenClaw 同样规则清洗后仍能对上
assert.equal(outer("call/abc 1", "openclaw:core:exec", { command: "Start-Process notepad" }), undefined);
assert.equal(inner("tool_search_code:call_abc_1:exec:1", "exec", { command: "Start-Process notepad" }), undefined);
assert.equal(lastAudit().decision, "allow-inner-dedup");

// ---------- 4. client 来源不放行 ----------
const rc = outer("c_cl", "client:client:read", { path: "x.txt" });
assert.ok(rc.requireApproval, "client 来源即使与 L0 工具同名也走 L3");
a = lastAudit();
assert.equal(a.source, "client");
assert.equal(a.level, "L3");

// ---------- 5. 直接调用（非 tool_call）行为不变 ----------
assert.equal(g.beforeToolCall({ toolName: "ls", params: { path: "C:\\" } }, { sessionKey: "s1" }), undefined);
assert.equal((g.beforeToolCall({ toolName: "exec", params: off }, { sessionKey: "s1" }) as any).requireApproval.timeoutMs, 120_000);

console.log("PASS stage1 tool_call routing / inner dedup / client / timeout");
