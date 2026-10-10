// S3 插件自测：不依赖 OpenClaw，直接测纯逻辑与代理（假上游）。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { execFileSync } from "node:child_process";
import { classify, type LevelsFile } from "../plugins/guanjia-guard/src/levels";
import { createMasker, createStreamUnmasker, startRedactProxy } from "../plugins/guanjia-guard/src/redact";
import { createStore } from "../plugins/guanjia-guard/src/store";
import { createGuard } from "../plugins/guanjia-guard/src/guard";
import { onUserTurn } from "../plugins/guanjia-guard/src/habits";

const pluginDir = path.resolve(import.meta.dirname, "../plugins/guanjia-guard");
const levels = JSON.parse(fs.readFileSync(path.join(pluginDir, "defaults/levels.json"), "utf8")) as LevelsFile;
const results: string[] = [];
const ok = (name: string) => results.push("PASS " + name);

// ---------- 分级 ----------
const lv = (toolName: string, params: any, proactive = false, extra: any = {}) =>
  classify(levels, { toolName, params, ...extra }, proactive).level;
assert.equal(lv("read", { path: "x" }), "L0");
assert.equal(lv("exec", { command: "start steam://rungameid/570" }), "L1");
assert.equal(lv("exec", { command: "start x & del y" }), "L2", "带删除的拼接要先问");
assert.equal(lv("exec", { command: "start x ; echo 1" }), "L1", "v2：普通拼接直接做");
assert.equal(lv("exec", { command: "Remove-Item C:\\a -Recurse" }), "L2", "v2：删除口头先问");
assert.equal(lv("exec", { command: "dir C:\\" }), "L0");
assert.equal(lv("exec", { command: "python foo.py" }), "L1", "v2：运行已有脚本直接做");
assert.equal(lv("write", { path: "memory/timeline.jsonl" }), "L1");
assert.equal(lv("write", { path: "guanjia/levels.json" }), "L2", "v2：改管家规则口头先问");
assert.equal(lv("write", { path: "C:\\Users\\inksn\\.openclaw\\workspace\\guanjia\\levels.json" }), "L2");
assert.equal(lv("write", { path: "D:\\x.txt" }), "L1", "v2：写文件直接做");
assert.equal(lv("write", { path: "F:\\coding\\guanjia\\tmp\\s3\\l2-verify.txt" }), "L1", "项目 tmp 不算管家规则");
assert.equal(lv("write", { path: "./guanjia/levels.json" }), "L2");
assert.equal(lv("edit", { file_path: "C:/Users/inksn/.openclaw/workspace/guanjia/family-facts.json" }), "L2");
assert.equal(lv("apply_patch", {}, false, { derivedPaths: ["guanjia/levels.json"] }), "L2", "补丁动 guanjia/ 要先问");
assert.equal(lv("write", { path: "../x.txt" }), "L2", "写出工作区到 .openclaw 要先问");
assert.equal(lv("write", { path: "C:\\Users\\inksn\\.openclaw\\workspace\\notes.md" }), "L1");
assert.equal(lv("apply_patch", {}, false, { derivedPaths: ["a.md", "memory/b.md"] }), "L1");
assert.equal(lv("apply_patch", {}, false, { derivedPaths: ["a.md", "D:\\b.md"] }), "L1", "v2：补丁写工作区外的普通文件直接做");
assert.equal(lv("exec", { command: "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\inksn\\.openclaw\\workspace\\tools\\launch-app.ps1 -Name lol" }), "L1", "S2 启动器");
assert.equal(lv("exec", { command: "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\inksn\\.openclaw\\workspace\\tools\\launch-app.ps1 -Name lol; del x" }), "L2");
assert.equal(lv("automations", { action: "add" }), "L1", "S2 提醒");
assert.equal(lv("edit", { path: "memory/timeline.md" }), "L1");
assert.equal(lv("gateway", {}), "L2", "v2：网关配置口头先问");
assert.equal(lv("unknown_tool", {}), "L1", "v2：默认直接做");
assert.equal(lv("cron", {}, true), "L1", "v2：cron 会话不再整体降档");
assert.equal(lv("exec", { command: "python foo.py" }, true), "L1");
assert.equal(lv("read", {}, true), "L0");
// D8 覆盖：未过期生效，过期忽略，作用域限定
const withOv = (o: any) => ({ ...levels, overrides: [o] });
const future = new Date(Date.now() + 3600_000).toISOString();
const past = new Date(Date.now() - 1000).toISOString();
const rm = { toolName: "exec", params: { command: "Remove-Item D:\\x.txt" } };
assert.equal(classify(withOv({ tool: "exec", param: "command", pattern: "^remove-item ", level: "L1", expiresAt: future }), rm, false).level, "L1");
assert.equal(classify(withOv({ tool: "exec", level: "L1", expiresAt: past }), rm, false).level, "L2");
assert.equal(classify(withOv({ tool: "exec", level: "L1", expiresAt: future, scope: "session:a" }), { ...rm, sessionKey: "b" }, false).level, "L2");
assert.equal(classify(withOv({ tool: "exec", level: "L1", expiresAt: "" }), rm, false).level, "L2", "无有效期不生效");
ok("levels classify (32 cases)");

// ---------- 脱敏 ----------
const facts = { people: ["小红豆", "王大锤"], rooms: ["主卧"], devices: ["inksnow"], paths: ["C:\\Users\\inksn"], ips: ["192.168.10.8"] };
const m = createMasker(facts);
const counts: Record<string, number> = {};
const masked = m.mask("小红豆在主卧用 inksnow 打开 C:\\Users\\inksn\\a.txt，也试了 c:/users/inksn/b，IP 192.168.10.8", counts);
assert.ok(!/小红豆|主卧|inksnow|192\.168|inksn/i.test(masked), masked);
assert.deepEqual(counts, { 人名: 1, 房间: 1, 设备: 1, 路径: 2, IP: 1 });
assert.equal(m.unmask(masked).includes("C:\\Users\\inksn\\a.txt"), true);
// JSON 里的路径（工具参数）
const argJson = JSON.stringify({ path: "C:\\Users\\inksn\\x.md" });
const maskedArg = m.mask(argJson, {});
assert.ok(maskedArg.includes("[[路径1]]"), maskedArg);
assert.deepEqual(JSON.parse(m.unmask(maskedArg, true)), { path: "C:\\Users\\inksn\\x.md" });
// 流式切分
const su = createStreamUnmasker(m);
let out = su.push("0:content", "你好[[人", false);
out += su.push("0:content", "名1]]，去[", false);
out += su.push("0:content", "[房间1]]吧", false);
out += su.flush("0:").map(([, v]) => v).join("");
assert.equal(out, "你好小红豆，去主卧吧");
ok("masker + stream unmask");

// ---------- 代理（假上游） ----------
const tmpWs = fs.mkdtempSync(path.join(os.tmpdir(), "gj-"));
const store = createStore(tmpWs, path.join(pluginDir, "defaults"));
store.writeJson("family-facts.json", facts);
let seenBody = "";
const upstream = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    seenBody = b;
    const j = JSON.parse(b);
    if (j.stream) {
      res.writeHead(200, { "content-type": "text/event-stream" });
      const send = (o: any) => res.write(`data: ${JSON.stringify(o)}\n\n`);
      send({ id: "1", choices: [{ index: 0, delta: { content: "好的，[[人" } }] });
      send({ id: "1", choices: [{ index: 0, delta: { content: "名1]]在[[房间1" } }] });
      send({ id: "1", choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: "{\"path\":\"[[路径" } }] } }] });
      send({ id: "1", choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: "1]]\\\\a.md\"}" } }] } }] });
      send({ id: "1", choices: [{ index: 0, delta: { content: "]]" }, finish_reason: "stop" }] });
      res.end("data: [DONE]\n\n");
    } else {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ choices: [{ index: 0, message: { content: "[[人名2]] 收到", tool_calls: [{ function: { arguments: "{\"p\":\"[[路径1]]\"}" } }] } }] }));
    }
  });
});
await new Promise<void>((r) => upstream.listen(18899, "127.0.0.1", () => r()));
const stop = startRedactProxy({ port: 18898, upstreamOrigin: "http://127.0.0.1:18899", store, logger: { info() {}, warn: console.warn } });
await new Promise((r) => setTimeout(r, 200));
const reqBody = { model: "m", messages: [{ role: "system", content: "工作区 C:\\Users\\inksn\\.openclaw" }, { role: "user", content: "小红豆去主卧了吗" }] };

const r1 = await fetch("http://127.0.0.1:18898/v1/chat/completions", { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer t" }, body: JSON.stringify(reqBody) });
const j1 = await r1.json();
assert.ok(!/小红豆|主卧|inksn/.test(seenBody), "上游不应看到原文: " + seenBody);
assert.equal(j1.choices[0].message.content, "王大锤 收到");
assert.deepEqual(JSON.parse(j1.choices[0].message.tool_calls[0].function.arguments), { p: "C:\\Users\\inksn" });

const r2 = await fetch("http://127.0.0.1:18898/v1/chat/completions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...reqBody, stream: true }) });
const sse = await r2.text();
let content = "", args = "";
for (const line of sse.split("\n")) {
  if (!line.startsWith("data: ") || line.includes("[DONE]")) continue;
  const c = JSON.parse(line.slice(6)).choices[0];
  content += c.delta?.content ?? "";
  for (const tc of c.delta?.tool_calls ?? []) args += tc.function.arguments;
}
assert.equal(content, "好的，小红豆在主卧");
assert.deepEqual(JSON.parse(args), { path: "C:\\Users\\inksn\\a.md" });
const audits = fs.readFileSync(store.file("audit.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l)).filter((a) => a.kind === "cloud");
assert.equal(audits.length, 2);
assert.ok(!JSON.stringify(audits).includes("小红豆"), "审计不含原文");
assert.equal(audits[0].masked["人名"], 1);
await stop();
upstream.close();
ok("redact proxy json + sse + audit");

// ---------- guard L2 口头确认 ----------
const g = createGuard(store, levels);
const ev = { toolName: "exec", params: { command: "Remove-Item D:\\x.txt" } };
const r = g.beforeToolCall(ev, { sessionKey: "s1", runId: "r1" }) as any;
assert.equal(r.block, true);
assert.match(r.blockReason, /需要确认/);
assert.ok(g.onUserTurn("s1", "确认"));
const retry = { toolName: "exec", params: { command: "remove-item d:/x.txt ", timeoutSeconds: 30 } };
assert.equal(g.beforeToolCall(retry, { sessionKey: "s1", runId: "r2" }), undefined, "确认后同目标放行（容忍斜杠/大小写/多带参数）");
assert.equal((g.beforeToolCall(ev, { sessionKey: "s1", runId: "r3" }) as any).block, true, "放行只一次");
g.onUserTurn("s1", "确认");
assert.equal((g.beforeToolCall({ toolName: "exec", params: { command: "Remove-Item D:\\other.txt" } }, { sessionKey: "s1" }) as any).block, true, "确认不串到别的目标");
g.noteRun("rc", "cron");
assert.equal(g.beforeToolCall({ toolName: "cron", params: {} }, { sessionKey: "agent:main:x", runId: "rc" }), undefined, "v2：cron 会话里直接做的照做");
assert.equal((g.beforeToolCall(ev, { sessionKey: "agent:main:x", runId: "rc" }) as any).block, true, "cron 会话里先问的直接拦");
const gw = g.beforeToolCall({ toolName: "gateway", params: { action: "restart" } }, { sessionKey: "s1" }) as any;
assert.equal(gw.block, true, "v2：网关重启口头先问");
assert.equal(gw.requireApproval, undefined, "v2：不再走带外审批");
ok("guard L2 confirm / proactive / L3");

// ---------- 习惯抽取脚本 + 确认流 ----------
fs.mkdirSync(path.join(tmpWs, "memory"), { recursive: true });
const lines: string[] = [];
const base = new Date(); base.setHours(19, 32, 0, 0);
for (let w = 1; w <= 4; w++) {
  const d = new Date(base.getTime() - w * 7 * 86400000);
  lines.push(JSON.stringify({ ts: d.toISOString(), text: "倒垃圾" }));
}
lines.push(JSON.stringify({ ts: new Date(Date.now() - 86400000).toISOString(), text: "一次性的事" }));
fs.writeFileSync(path.join(tmpWs, "memory", "timeline.jsonl"), lines.join("\n"));
// S2 的 timeline.md 格式：三周同一天晚上八点的交房租提醒 + 一条取消 + 一条记事
const md = ["# 时间线", ""];
for (let w = 1; w <= 3; w++) {
  const rec = new Date(base.getTime() - w * 7 * 86400000 - 86400000);
  const at = new Date(base.getTime() - w * 7 * 86400000); at.setHours(20, 0, 0, 0);
  md.push(`- ${rec.toISOString()} | 来源:语音 | 类型:提醒 | 原话:"记一下明天晚上八点交房租" | 时间:${at.toISOString()} | 任务:job-${w}`);
}
md.push(`- ${new Date().toISOString()} | 来源:微信 | 类型:取消 | 原话:"不要了" | 任务:job-1`);
md.push(`- ${new Date().toISOString()} | 来源:语音 | 类型:记事 | 原话:"车钥匙在抽屉"`);
fs.writeFileSync(path.join(tmpWs, "memory", "timeline.md"), md.join("\n"));
const outText = execFileSync(process.execPath, [path.join(pluginDir, "scripts/habit-extract.mjs")], { env: { ...process.env, GUANJIA_WORKSPACE: tmpWs } }).toString();
assert.match(outText, /added=2/, outText);
const drafts = store.readJson<any>("habit-drafts.json", {});
const trash = drafts.drafts.find((d: any) => d.text === "倒垃圾");
const rent = drafts.drafts.find((d: any) => d.text === "交房租");
assert.ok(trash && rent, JSON.stringify(drafts.drafts));
assert.equal(trash.time, "19:30");
assert.equal(trash.weekday, base.getDay());
assert.equal(rent.time, "20:00");
// 再跑一次不重复；只保留倒垃圾一条继续测确认流
execFileSync(process.execPath, [path.join(pluginDir, "scripts/habit-extract.mjs")], { env: { ...process.env, GUANJIA_WORKSPACE: tmpWs } });
assert.equal(store.readJson<any>("habit-drafts.json", {}).drafts.length, 2);
store.writeJson("habit-drafts.json", { drafts: [trash], rejected: [] });

const noon = new Date(); noon.setHours(12, 0, 0, 0);
const created: any[] = [];
const add = async (input: any) => { created.push(input); return { id: "job-1" }; };
const ask = await onUserTurn(store, "今天天气怎么样", add, "Asia/Shanghai", noon);
assert.match(ask!, /要不要以后每周.+19:30 提醒你倒垃圾/);
assert.equal(await onUserTurn(store, "再说一遍", add, "Asia/Shanghai", new Date(noon.getTime() + 1000)), undefined, "答非所问不纠缠、当天不再问");
const yes = await onUserTurn(store, "好的", add, "Asia/Shanghai", new Date(noon.getTime() + 2000));
assert.match(yes!, /已创建/);
assert.equal(created[0].schedule.expr, `30 19 * * ${base.getDay()}`);
assert.equal(store.readJson<any>("habit-drafts.json", {}).drafts[0].status, "accepted");
// 否决路径
const f2 = store.readJson<any>("habit-drafts.json", {});
f2.drafts.push({ key: "k2", weekday: 1, time: "08:00", text: "吃药", weeks: 3, status: "draft" });
f2.lastAskDate = "2000-01-01";
store.writeJson("habit-drafts.json", f2);
await onUserTurn(store, "早", add, "Asia/Shanghai", noon);
const no = await onUserTurn(store, "不用了", add, "Asia/Shanghai", new Date(noon.getTime() + 1000));
assert.match(no!, /否决/);
assert.ok(store.readJson<any>("habit-drafts.json", {}).rejected.includes("k2"));
// 深夜不问
const f3 = store.readJson<any>("habit-drafts.json", {});
f3.drafts.push({ key: "k3", weekday: 2, time: "09:00", text: "开会", weeks: 3, status: "draft" });
f3.lastAskDate = "2000-01-01";
store.writeJson("habit-drafts.json", f3);
const late = new Date(); late.setHours(23, 30, 0, 0);
assert.equal(await onUserTurn(store, "睡了", add, "Asia/Shanghai", late), undefined);
ok("habit extract + ask/yes/no/late-night");

// ---------- 天气骨架 ----------
const wOut = execFileSync(process.execPath, [path.join(pluginDir, "scripts/weather-check.mjs")], { env: { ...process.env, GUANJIA_WORKSPACE: tmpWs, GUANJIA_WEATHER_API_KEY: "" } }).toString();
assert.match(wOut, /skipped/);
ok("weather skeleton no-key skip");

console.log(results.join("\n"));
