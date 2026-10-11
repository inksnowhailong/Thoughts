// exec 的 `ask` 参数：守卫在 before_tool_call 里把它改成 "off"（不请求 OpenClaw 自带的 exec 审批），审计记原值；
// 参数改写按 OpenClaw 2026.9.6 的方式：钩子返回 { params }，宿主把它浅合并到原参数上（{ ...原参数, ...返回的 params }），
// 所以不能靠"漏掉这个键"来删除，只能把值改成不加严的 "off"。
// 运行：cd F:\coding\guanjia\openclaw && npx --yes tsx test\perm-v2c-exec-ask.mts
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createGuard } from "../plugins/guanjia-guard/src/guard";
import { createStore } from "../plugins/guanjia-guard/src/store";
import type { LevelsFile } from "../plugins/guanjia-guard/src/levels";

const pluginDir = path.resolve(import.meta.dirname, "../plugins/guanjia-guard");
const levels = JSON.parse(fs.readFileSync(path.join(pluginDir, "defaults/levels.json"), "utf8")) as LevelsFile;
const store = createStore(fs.mkdtempSync(path.join(os.tmpdir(), "gj-v2c-")), path.join(pluginDir, "defaults"));
const g = createGuard(store, levels);
const lastAudit = () => JSON.parse(fs.readFileSync(store.file("audit.jsonl"), "utf8").trim().split("\n").at(-1)!);
const SK = "agent:main:main";
const call = (params: Record<string, unknown>, extra: Record<string, unknown> = {}, ctx: any = { sessionKey: SK }) =>
  g.beforeToolCall({ toolName: "exec", params, ...extra }, ctx) as any;
/** 宿主怎么用钩子结果：{ ...原参数, ...返回的 params } */
const applied = (orig: Record<string, unknown>, r: any) => (r?.params ? { ...orig, ...r.params } : orig);

// ---------- 1. 带 ask 的普通命令：ask 改成 off，其它参数一字不动，审计记原值 ----------
const p1 = { command: "Get-Date -Format HH:mm", timeoutSeconds: 15, yieldMs: 500, ask: "always" };
let r = call(p1);
assert.equal(r.block, undefined, "普通命令照样放行");
assert.deepEqual(r.params, { ask: "off" }, "只返回要改的一个键");
assert.deepEqual(applied(p1, r), { command: "Get-Date -Format HH:mm", timeoutSeconds: 15, yieldMs: 500, ask: "off" }, "合并后只有 ask 变了");
assert.equal(p1.ask, "always", "不改调用方传进来的对象");
let a = lastAudit();
assert.equal(a.decision, "allow");
assert.equal(a.askStripped, "always", "审计记原值");
assert.deepEqual(a.paramKeys, ["ask", "command", "timeoutSeconds", "yieldMs"], "paramKeys 照旧列出收到的键（含 ask）");

r = call({ command: "Get-Date", ask: "on-miss" });
assert.deepEqual(r.params, { ask: "off" });
assert.equal(lastAudit().askStripped, "on-miss");

// ---------- 2. ask 本来就是 off / 没有 ask / 不是 exec：不改写、不记 ----------
r = call({ command: "Get-Date", ask: "off" });
assert.equal(r, undefined, "ask=off 不用改");
assert.equal(lastAudit().askStripped, undefined);
r = call({ command: "Get-Date" });
assert.equal(r, undefined, "没有 ask 原样放行");
assert.equal(lastAudit().askStripped, undefined);
r = g.beforeToolCall({ toolName: "write", params: { path: "notes.md", content: "x", ask: "always" } }, { sessionKey: SK }) as any;
assert.equal(r, undefined, "别的工具的 ask 字段不碰");

// ---------- 3. 不影响「需要确认」：危险命令带 ask 照样被拦、先口头问；同意后重试放行并同样去掉 ask ----------
const danger = { command: "Remove-Item D:\\old\\a.txt", ask: "always" };
r = call(danger, { toolCallId: "d1" });
assert.equal(r.block, true, "危险命令仍被拦");
assert.match(r.blockReason, /^需要确认/);
assert.equal(r.requireApproval, undefined);
assert.equal(r.params, undefined, "拦下时不返回改写");
assert.equal(lastAudit().decision, "block-need-confirm");
assert.ok(g.onUserTurn(SK, "允许"), "主人口头同意");
r = call(danger, { toolCallId: "d2" });
assert.equal(r.block, undefined, "同意后同参数放行");
assert.deepEqual(applied(danger, r), { command: "Remove-Item D:\\old\\a.txt", ask: "off" }, "放行时 ask 也被去掉");
a = lastAudit();
assert.equal(a.decision, "allow-confirmed");
assert.equal(a.askStripped, "always");
// 没有 ask 的危险命令：行为与以前完全一样
assert.equal(call({ command: "Remove-Item D:\\old\\b.txt" }).block, true);
assert.equal(lastAudit().askStripped, undefined);

// ---------- 4. 经 tool_call：外层不改（结构不同），内层 exec 命中去重登记时去掉 ask ----------
const nested = { command: "Get-Date", ask: "always" };
let o = g.beforeToolCall({ toolName: "tool_call", params: { id: "openclaw:core:exec", args: nested }, toolCallId: "o1" }, { sessionKey: SK }) as any;
assert.equal(o, undefined, "外层放行，不改写 tool_call 的参数");
const inn = g.beforeToolCall({ toolName: "exec", params: nested, toolCallId: "tool_search_code:o1:exec:1" }, undefined) as any;
assert.deepEqual(inn.params, { ask: "off" }, "内层 exec 才是真正执行的那次，在这里去掉 ask");
a = lastAudit();
assert.equal(a.decision, "allow-inner-dedup");
assert.equal(a.askStripped, "always");

// ---------- 5. 定时任务 / 心跳：行为不变（L1 直接做仍去掉 ask；L2 仍拦下） ----------
g.noteRun("rc", "cron");
r = call({ command: "Get-Date", ask: "always" }, {}, { sessionKey: "agent:main:cron:j1", runId: "rc" });
assert.deepEqual(r.params, { ask: "off" });
assert.equal(call({ command: "Remove-Item D:\\x.txt", ask: "always" }, {}, { sessionKey: "agent:main:cron:j1", runId: "rc" }).block, true);

// ---------- 6. 源码里钩子改参数的方式：只在 guard.ts 一处，值固定 off ----------
const src = fs.readFileSync(path.join(pluginDir, "src/guard.ts"), "utf8");
assert.match(src, /askStripped/);
assert.equal((src.match(/ask: "off"/g) ?? []).length, 1, "ask 只会被改成 off 这一个值");

console.log("PASS perm v2c exec ask strip");
