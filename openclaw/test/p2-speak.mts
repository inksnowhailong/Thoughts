// P2「说出来」工具与分级：speak 工具（成功 / 守护进程没跑 / 静音 / 参数）、分级 speak=L1（直接调与经 tool_call）、语音轮标记剥离后口头确认仍有效。
// 运行：cd F:\coding\guanjia\openclaw && npx --yes tsx test\p2-speak.mts
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import type { AddressInfo } from "node:net";
import { createSpeakTool, stripVoiceTag, DEFAULT_SPEAK_URL, SPEAK_MAX_CHARS } from "../plugins/guanjia-guard/src/speak";
import { classify, type LevelsFile } from "../plugins/guanjia-guard/src/levels";
import { createGuard, isConfirmation } from "../plugins/guanjia-guard/src/guard";
import { onUserTurn as habitOnUserTurn } from "../plugins/guanjia-guard/src/habits";
import { createStore } from "../plugins/guanjia-guard/src/store";

const pluginDir = path.resolve(import.meta.dirname, "../plugins/guanjia-guard");
const levels = JSON.parse(fs.readFileSync(path.join(pluginDir, "defaults/levels.json"), "utf8")) as LevelsFile;
const manifest = JSON.parse(fs.readFileSync(path.join(pluginDir, "openclaw.plugin.json"), "utf8"));

// ---------- 1. 清单与工具形状 ----------
assert.deepEqual(manifest.contracts?.tools, ["speak"], "清单声明 speak");
assert.equal(manifest.configSchema.properties.speakUrl.type, "string");
assert.equal(DEFAULT_SPEAK_URL, "http://127.0.0.1:18795/say");
const t0 = createSpeakTool() as any;
assert.equal(t0.name, "speak");
assert.deepEqual(t0.parameters.required, ["text"]);
assert.match(t0.description, /Speak/);

// ---------- 2. 假 fetch ----------
const calls: any[] = [];
const fakeFetch = (status: number, body: unknown) => async (url: string, init: any) => {
  calls.push({ url, init });
  return { status, text: async () => JSON.stringify(body) };
};
let r: any = await (createSpeakTool({ fetchImpl: fakeFetch(200, { ok: true, reason: "queued" }) }) as any).execute("c1", { text: "  今天降温了，多穿点。 " });
assert.equal(r.details.ok, true);
assert.equal(calls[0].url, DEFAULT_SPEAK_URL);
assert.equal(calls[0].init.method, "POST");
assert.equal(calls[0].init.headers["Content-Type"], "application/json");
assert.deepEqual(JSON.parse(calls[0].init.body), { text: "今天降温了，多穿点。" });
assert.match(r.content[0].text, /已让电脑念出来/);

r = await (createSpeakTool({ fetchImpl: fakeFetch(409, { ok: false, reason: "muted" }) }) as any).execute("c2", { text: "x" });
assert.equal(r.details.reason, "muted");
assert.match(r.content[0].text, /静音/);
r = await (createSpeakTool({ fetchImpl: fakeFetch(409, { ok: false, reason: "gaming" }) }) as any).execute("c2b", { text: "x" });
assert.equal(r.details.reason, "gaming");
assert.match(r.content[0].text, /打游戏/);
r = await (createSpeakTool({ fetchImpl: async () => { throw new TypeError("fetch failed"); } }) as any).execute("c3", { text: "x" });
assert.equal(r.details.reason, "daemon_down");
assert.match(r.content[0].text, /没有在运行/);
const before = calls.length;
r = await (createSpeakTool({ fetchImpl: fakeFetch(200, { ok: true }) }) as any).execute("c4", { text: "   " });
assert.equal(r.details.reason, "empty");
r = await (createSpeakTool({ fetchImpl: fakeFetch(200, { ok: true }) }) as any).execute("c5", { text: "字".repeat(SPEAK_MAX_CHARS + 1) });
assert.equal(r.details.reason, "too_long");
r = await (createSpeakTool({ fetchImpl: fakeFetch(200, { ok: true }) }) as any).execute("c6", { text: 123 });
assert.equal(r.details.reason, "empty");
assert.equal(calls.length, before, "参数不合法时不发请求");

// ---------- 3. 真 HTTP（本机临时端口，模拟语音守护进程）----------
const got: any[] = [];
const srv = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    got.push({ method: req.method, url: req.url, ct: req.headers["content-type"], body: JSON.parse(b) });
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, reason: "queued" }));
  });
});
await new Promise<void>((ok) => srv.listen(0, "127.0.0.1", () => ok()));
const port = (srv.address() as AddressInfo).port;
r = await (createSpeakTool({ url: `http://127.0.0.1:${port}/say` }) as any).execute("c7", { text: "管家上线了" });
assert.equal(r.details.ok, true);
assert.deepEqual(got[0], { method: "POST", url: "/say", ct: "application/json", body: { text: "管家上线了" } });
await new Promise<void>((ok) => srv.close(() => ok()));
r = await (createSpeakTool({ url: `http://127.0.0.1:${port}/say`, timeoutMs: 1500 }) as any).execute("c8", { text: "x" });
assert.equal(r.details.reason, "daemon_down", "端口没人听时明确报守护进程没跑");

// ---------- 4. 语音轮标记 ----------
assert.equal(stripVoiceTag("[语音 2026-10-10 18:05 周六] 确认"), "确认");
assert.equal(stripVoiceTag("[Sat 2026-10-10 18:05:01 GMT+8] [语音 2026-10-10 18:05 周六] 确认"), "确认");
assert.equal(stripVoiceTag("确认"), "确认");
assert.equal(stripVoiceTag("[微信] 确认"), "[微信] 确认");

// ---------- 5. 分级 ----------
assert.equal(levels.tools.speak, "L1");
assert.equal(classify(levels, { toolName: "speak", params: { text: "x" } }, false).level, "L1");
const store = createStore(fs.mkdtempSync(path.join(os.tmpdir(), "gj-p2-")), path.join(pluginDir, "defaults"));
const g = createGuard(store, levels);
const lastAudit = () => JSON.parse(fs.readFileSync(store.file("audit.jsonl"), "utf8").trim().split("\n").at(-1)!);
assert.equal(g.beforeToolCall({ toolName: "speak", params: { text: "你好" } }, { sessionKey: "s1" }), undefined, "直接调 speak 放行");
assert.equal(lastAudit().level, "L1");
assert.equal(g.beforeToolCall({ toolName: "tool_call", params: { id: "openclaw:guanjia-guard:speak", args: { text: "你好" } }, toolCallId: "c_sp" }, { sessionKey: "s1", runId: "r1" }), undefined);
let a = lastAudit();
assert.equal(a.tool, "speak");
assert.equal(a.via, "tool_call");
assert.equal(a.catalogSource, "openclaw");
assert.equal(a.decision, "allow");
assert.equal(g.beforeToolCall({ toolName: "speak", params: { text: "你好" }, toolCallId: "tool_search_code:c_sp:speak:1" }, undefined), undefined);
assert.equal(lastAudit().decision, "allow-inner-dedup");
assert.equal(g.beforeToolCall({ toolName: "tool_call", params: { id: "speak", args: { text: "你好" } }, toolCallId: "c_sp2" }, { sessionKey: "s1" }), undefined, "裸名 speak 在分级表里，不会被强制 L3");
assert.equal(lastAudit().level, "L1");

// ---------- 6. 语音轮里的口头确认 ----------
const w = { path: "D:\\x.txt", content: "hi" };
assert.equal((g.beforeToolCall({ toolName: "write", params: w }, { sessionKey: "s9" }) as any).block, true);
assert.ok(g.onUserTurn("s9", "[语音 2026-10-10 18:05 周六] 确认"), "带语音标记的确认也算确认");
assert.equal(g.beforeToolCall({ toolName: "write", params: w }, { sessionKey: "s9" }), undefined);

// ---------- 7. 语音里像确认、其实不是的话 ----------
const V = "[语音 2026-10-10 18:05 周六] ";
for (const yes of ["确认", "确定", "好", "好的", "好的，删吧", "好吧", "可以", "可以的", "行", "对", "是的", "OK", "yes", "嗯，好的", "嗯好", "确认删除", "执行吧", "同意"]) {
  assert.ok(isConfirmation(V + yes), `应算确认：${yes}`);
}
for (const no of ["好了好了", "好了", "嗯", "嗯，不要了", "好，不要了", "可以了，停吧", "行了，别弄了", "好吃吗", "对不对", "算了", "等等", "不", "停", "确认一下是哪个文件", "可以吗"]) {
  assert.ok(!isConfirmation(V + no), `不应算确认：${no}`);
}
assert.equal((g.beforeToolCall({ toolName: "write", params: w }, { sessionKey: "s10" }) as any).block, true);
assert.equal(g.onUserTurn("s10", V + "好了好了"), undefined, "叫停的话不放行 L2");
assert.equal((g.beforeToolCall({ toolName: "write", params: w }, { sessionKey: "s10" }) as any).block, true, "叫停后同参数重试仍要确认");

// ---------- 8. 习惯草案：新问题不能被当成"答应" ----------
const hStore = createStore(fs.mkdtempSync(path.join(os.tmpdir(), "gj-p2h-")), path.join(pluginDir, "defaults"));
const hNow = new Date(); hNow.setHours(12, 0, 0, 0);
const hAdds: any[] = [];
const hAdd = async (input: any) => { hAdds.push(input); return { id: "job-h" }; };
for (const [i, text] of ["要不要带伞", "嗯，今天天气怎么样", "好了好了", "对不对"].entries()) {
  hStore.writeJson("habit-drafts.json", { drafts: [{ key: `h${i}`, weekday: 1, time: "08:00", text: "吃药", weeks: 3, status: "asked", askedAt: hNow.toISOString() }], rejected: [] });
  await habitOnUserTurn(hStore, text, hAdd, "Asia/Shanghai", new Date(hNow.getTime() + 1000));
}
assert.equal(hAdds.length, 0, "新问题/叫停不建周期提醒");
hStore.writeJson("habit-drafts.json", { drafts: [{ key: "hy", weekday: 1, time: "08:00", text: "吃药", weeks: 3, status: "asked", askedAt: hNow.toISOString() }], rejected: [] });
assert.match((await habitOnUserTurn(hStore, "好的", hAdd, "Asia/Shanghai", new Date(hNow.getTime() + 1000)))!, /已创建/);

// ---------- 9. 守护进程排满时的提示 ----------
r = await (createSpeakTool({ fetchImpl: fakeFetch(409, { ok: false, reason: "busy" }) }) as any).execute("c9", { text: "x" });
assert.equal(r.details.reason, "busy");
assert.match(r.content[0].text, /排着/);

console.log("PASS p2 speak tool / levels / voice tag / confirm phrases / habit answers");
