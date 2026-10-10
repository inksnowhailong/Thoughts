// 阶段 1 tool_call 定级用例（确认策略 v2 改期望）：外层按真实工具定级、内层去重、真实目标无法确定时拒绝（不再带外审批）。
// 运行：cd F:\coding\guanjia\openclaw && npx --yes tsx test\stage1-toolcall.mts
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { resolveToolCallTarget, type LevelsFile } from "../plugins/guanjia-guard/src/levels";
import { createGuard, parentToolCallIdOf, sanitizeToolCallIdPart } from "../plugins/guanjia-guard/src/guard";
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
assert.equal(outer("c_np", "openclaw:core:exec", { command: "Start-Process notepad" }), undefined, "打开记事本直接做");
let a = lastAudit();
assert.equal(a.tool, "exec");
assert.equal(a.level, "L1");
assert.equal(a.via, "tool_call");
assert.equal(a.catalogSource, "openclaw");
assert.equal(a.decision, "allow");
assert.equal(outer("c_poll", "openclaw:core:process", { action: "poll", sessionId: "x" }), undefined, "process poll 经 tool_call 也是 L0");
assert.equal(outer("c_web", "openclaw:core:web_fetch", { url: "https://wttr.in/?format=3" }), undefined, "web_fetch 经 tool_call 放行");
// v2：解析不出目标时按 tool_call 本身定级，默认直接做（OpenClaw 自己会因为没有 id 报错）
assert.equal(g.beforeToolCall({ toolName: "tool_call", params: { args: {} }, toolCallId: "c_noid" }, { sessionKey: "s0" }), undefined);

// ---------- 3. 内层去重：同一次调用只确认一次 ----------
// 先问的事经 tool_call：外层口头确认一次；内层的 ctx 没有 sessionKey，不去重就会被判"无人确认"拦下
const w = { command: "Remove-Item D:\\x.txt" };
assert.equal(outer("c_w1", "openclaw:core:exec", w, "s2").block, true);
assert.equal(lastAudit().decision, "block-need-confirm");
assert.ok(g.onUserTurn("s2", "允许"));
assert.equal(outer("c_w2", "openclaw:core:exec", w, "s2"), undefined, "确认后外层同参放行");
assert.equal(lastAudit().decision, "allow-confirmed");
assert.equal(inner("tool_search_code:c_w2:exec:1", "exec", w), undefined, "内层凭外层登记放行，不要求第二次确认");
assert.equal(lastAudit().decision, "allow-inner-dedup");
assert.equal(inner("tool_search_code:c_w2:exec:2", "exec", w).block, true, "登记只能用一次");

// 关机经 tool_call：v2 改成口头确认（不再带外审批）
const off = { command: "shutdown /s /t 1800" };
const r3 = outer("c_s1", "openclaw:core:exec", off);
assert.equal(r3.block, true, "关机先口头问");
assert.equal(r3.requireApproval, undefined, "不再带外审批");
assert.ok(g.onUserTurn("s1", "可以"));
assert.equal(outer("c_s1b", "openclaw:core:exec", off), undefined);
assert.equal(inner("tool_search_code:c_s1b:exec:1", "exec", off), undefined, "确认后内层不再第二次问");
// 没确认（说了别的）不留登记
assert.equal(outer("c_s2", "openclaw:core:exec", off).block, true);
g.onUserTurn("s1", "等等");
assert.equal(inner("tool_search_code:c_s2:exec:1", "exec", off, { sessionKey: "s1" }).block, true, "没确认就没有放行登记");
// 没有外层登记的内层（如 tool_search_code 代码模式）按真实工具正常定级
assert.equal(inner("tool_search_code:code_9:exec:1", "exec", off, { sessionKey: "s1" }).block, true);
// 工具名对不上不去重
assert.equal(outer("c_m", "openclaw:core:exec", { command: "Start-Process notepad" }), undefined);
assert.equal(inner("tool_search_code:c_m:exec:1", "exec", w, { sessionKey: "s3" }).block, true);
// 外层 id 带特殊字符：按 OpenClaw 同样规则清洗后仍能对上
assert.equal(outer("call/abc 1", "openclaw:core:exec", { command: "Start-Process notepad" }), undefined);
assert.equal(inner("tool_search_code:call_abc_1:exec:1", "exec", { command: "Start-Process notepad" }), undefined);
assert.equal(lastAudit().decision, "allow-inner-dedup");

// ---------- 4. client 来源：拒绝 ----------
const rc = outer("c_cl", "client:client:read", { path: "x.txt" });
assert.equal(rc.block, true, "client 来源即使与 L0 工具同名也拒绝");
assert.equal(rc.requireApproval, undefined);
a = lastAudit();
assert.equal(a.source, "client");
assert.equal(a.level, "L3");
assert.equal(a.decision, "block-refused");

// ---------- 5. 直接调用（非 tool_call） ----------
assert.equal(g.beforeToolCall({ toolName: "ls", params: { path: "C:\\" } }, { sessionKey: "s1" }), undefined);
assert.equal((g.beforeToolCall({ toolName: "exec", params: off }, { sessionKey: "s1" }) as any).block, true);

// ---------- 6. 选择器矛盾、裸名字、大小写/别名：拒绝 ----------
const amb = g.beforeToolCall({ toolName: "tool_call", params: { id: "tool_search", name: "exec", command: "shutdown /s /t 0" }, toolCallId: "c_amb" }, { sessionKey: "s1" }) as any;
assert.equal(amb.block, true, "选择器矛盾必须拒绝");
assert.equal(lastAudit().level, "L3");
const door = g.beforeToolCall({ toolName: "tool_call", params: { id: "open_door" }, toolCallId: "c_door" }, { sessionKey: "s1" }) as any;
assert.equal(door.block, true, "不在分级表里的裸名字必须拒绝");
assert.match(door.blockReason, /openclaw:core:exec/, "拒绝时告诉模型用完整 id");
for (const id of ["Read", "bash"]) {
  const mismatched = g.beforeToolCall({ toolName: "tool_call", params: { id }, toolCallId: `c_${id}` }, { sessionKey: "s1" }) as any;
  assert.equal(mismatched.block, true, `${id} 必须拒绝`);
  assert.equal(lastAudit().level, "L3");
}
assert.equal(g.beforeToolCall({ toolName: "tool_call", params: { id: "web_fetch" }, toolCallId: "c_wf" }, { sessionKey: "s1" }), undefined, "分级表里有的裸名字仍按原级别");
assert.equal(lastAudit().tool, "web_fetch");
assert.equal(lastAudit().level, "L0");

// ---------- 7. 参数不同不去重；登记 60 秒后失效 ----------
const notepad = { command: "Start-Process notepad" };
assert.equal(outer("c_fp", "openclaw:core:exec", notepad), undefined, "外层登记打开记事本");
const fpMismatch = inner("tool_search_code:c_fp:exec:1", "exec", off, { sessionKey: "s1" });
assert.equal(fpMismatch.block, true, "同一外层 id 下内层传关机参数不能去重");
assert.notEqual(lastAudit().decision, "allow-inner-dedup");
assert.equal(outer("c_ttl_ok", "openclaw:core:exec", notepad), undefined);
const now = Date.now;
try {
  Date.now = () => now() + 59_000;
  assert.equal(inner("tool_search_code:c_ttl_ok:exec:1", "exec", notepad), undefined);
  assert.equal(lastAudit().decision, "allow-inner-dedup", "59 秒内仍然去重");
} finally {
  Date.now = now;
}
assert.equal(outer("c_ttl", "openclaw:core:exec", notepad), undefined);
try {
  Date.now = () => now() + 61_000;
  assert.equal(inner("tool_search_code:c_ttl:exec:1", "exec", notepad, { sessionKey: "s1" }), undefined, "过期后按真实工具定级");
  assert.equal(lastAudit().decision, "allow", "登记 60 秒后失效");
  assert.notEqual(lastAudit().source, "outer-grant");
} finally {
  Date.now = now;
}

console.log("PASS stage1 tool_call routing / inner dedup / refuse unknown target");
