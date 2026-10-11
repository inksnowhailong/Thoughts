// 权限 v2 补丁：常用工具直接给模型（catalogMode: direct-only）、不走微信审批、规则段 / 技能口径、网关补丁脚本。
// 运行：cd F:\coding\guanjia\openclaw && npx --yes tsx test\perm-v2b.mts
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createGuard } from "../plugins/guanjia-guard/src/guard";
import { createStore } from "../plugins/guanjia-guard/src/store";
import type { LevelsFile } from "../plugins/guanjia-guard/src/levels";
import { createGetLocationTool, createGetWeatherTool, createLaunchAppTool } from "../plugins/guanjia-guard/src/ps-tools";
import { createSpeakTool } from "../plugins/guanjia-guard/src/speak";

const root = path.resolve(import.meta.dirname, "..");
const pluginDir = path.join(root, "plugins/guanjia-guard");
const WS = "C:\\Users\\inksn\\.openclaw\\workspace";

// ---------- 1. 四个专用工具带 catalogMode: direct-only（OpenClaw 工具搜索不把它们收进目录，直接出现在工具列表里）----------
const tools: any[] = [createSpeakTool({}), createLaunchAppTool({ workspaceDir: WS }), createGetLocationTool({ workspaceDir: WS }), createGetWeatherTool({ workspaceDir: WS })];
assert.deepEqual(tools.map((t) => t.name), ["speak", "launch_app", "get_location", "get_weather"]);
for (const t of tools) assert.equal(t.catalogMode, "direct-only", `${t.name} 应是 direct-only`);
const manifest = JSON.parse(fs.readFileSync(path.join(pluginDir, "openclaw.plugin.json"), "utf8"));
assert.deepEqual(manifest.contracts.tools, tools.map((t) => t.name), "清单与工具一致");
const schemaChars = tools.map((t) => JSON.stringify({ name: t.name, description: t.description, parameters: t.parameters }).length);
console.log(`direct-only 工具 schema 字符数：${tools.map((t, i) => `${t.name}=${schemaChars[i]}`).join(" ")} 合计=${schemaChars.reduce((a, b) => a + b, 0)}`);

// ---------- 2. 守卫仍然从不用 requireApproval（所有"需要确认"都是工具返回 → 模型口头问 → 主人说允许），直接调用与 tool_call 两条路都一样 ----------
const levels = JSON.parse(fs.readFileSync(path.join(pluginDir, "defaults/levels.json"), "utf8")) as LevelsFile;
const store = createStore(fs.mkdtempSync(path.join(os.tmpdir(), "gj-v2b-")), path.join(pluginDir, "defaults"));
const g = createGuard(store, levels);
for (const name of ["launch_app", "speak", "get_weather", "get_location"]) {
  const direct = g.beforeToolCall({ toolName: name, params: name === "speak" ? { text: "你好" } : { name: "lol" }, toolCallId: `d_${name}` }, { sessionKey: "agent:main:main" });
  assert.equal(direct, undefined, `${name} 直接调用放行`);
}
const askAny = g.beforeToolCall({ toolName: "exec", params: { command: "Remove-Item -Recurse -Force D:\\old" }, toolCallId: "x1" }, { sessionKey: "agent:main:main" }) as any;
assert.ok(askAny?.block, "危险命令被拦下");
assert.match(askAny.blockReason, /^需要确认/, "拦下时返回「需要确认（…）」文本");
assert.equal(askAny.requireApproval, undefined, "不走 OpenClaw 插件审批（requireApproval）");
assert.match(askAny.blockReason, /不要让用户去微信或网页里点/, "提示里明确不要去微信点");
const pluginSrc = ["guard.ts", "levels.ts", "ps-tools.ts", "speak.ts", "remember.ts", "habits.ts", "store.ts"].map((f) => fs.readFileSync(path.join(pluginDir, "src", f), "utf8")).join("\n") + fs.readFileSync(path.join(pluginDir, "index.ts"), "utf8");
assert.doesNotMatch(pluginSrc.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, ""), /requireApproval/, "插件代码里除注释外不出现 requireApproval");

// ---------- 3. 规则段与技能口径 ----------
const agents = fs.readFileSync(path.join(root, "AGENTS-S2-section.md"), "utf8");
assert.match(agents, /只有工具真的返回"需要确认"时才能说需要确认；没调工具就不许说被拦、要审批/, "规则段有「不许编造拦截」一句");
assert.match(agents, /在工具列表里就直接调用同名工具；列表里没有时才用 tool_call/, "规则段：专用工具直接调，没有才 tool_call");
assert.match(agents, /web_fetch[^。]*一律用 tool_call/, "规则段：web_fetch 仍用 tool_call");
assert.ok(agents.split("\n").every((l) => !/用 tool_call 调 (speak|launch_app|get_weather|get_location)/.test(l)), "规则段不再要求 tool_call 调专用工具");
for (const f of ["guanjia-launch", "guanjia-lookup"]) {
  const s = fs.readFileSync(path.join(root, "workspace/skills", f, "SKILL.md"), "utf8");
  assert.doesNotMatch(s, /用 tool_call 调 (launch_app|get_weather|get_location)/, `${f} 不再要求 tool_call 调专用工具`);
}
assert.match(agents, /<!-- guanjia-s2:begin -->[\s\S]*<!-- guanjia-s2:end -->/, "标记完整");

// ---------- 4. 网关补丁脚本 ----------
const script = path.join(root, "perm-v2b-apply.mjs");
assert.ok(fs.existsSync(script), "perm-v2b-apply.mjs 存在");
assert.equal(spawnSync(process.execPath, ["--check", script]).status, 0, "语法检查通过");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "gj-v2b-cfg-"));
const cfgPath = path.join(tmp, "openclaw.json");
fs.writeFileSync(cfgPath, JSON.stringify({ approvals: { plugin: { enabled: true, mode: "targets", targets: [{ channel: "openclaw-weixin", to: "x@im.wechat", accountId: "a" }] } }, plugins: { entries: {} } }, null, 2));
const before = fs.readFileSync(cfgPath, "utf8");
let r = spawnSync(process.execPath, [script, "--check", "--config", cfgPath], { encoding: "utf8" });
assert.equal(r.status, 0, r.stderr);
assert.match(r.stdout, /approvals\.plugin/, "--check 说明要移除 approvals.plugin");
assert.match(r.stdout, /openclaw-weixin/, "--check 点出现在的微信目标");
assert.equal(fs.readFileSync(cfgPath, "utf8"), before, "--check 不写任何文件");
fs.writeFileSync(cfgPath, JSON.stringify({ plugins: { entries: {} } }));
r = spawnSync(process.execPath, [script, "--check", "--config", cfgPath], { encoding: "utf8" });
assert.equal(r.status, 0, r.stderr);
assert.match(r.stdout, /已经没有 approvals\.plugin/, "已经没有时幂等：不报错");
r = spawnSync(process.execPath, [script], { encoding: "utf8" });
assert.notEqual(r.status, 0, "不带参数不动手（必须明确 --apply / --check / --rollback）");

console.log("ALL PASS perm-v2b");
