// 确认策略 v2 的专用工具：launch_app / get_location / get_weather（参数校验、结果解析、真 execFile 不经 shell、清单声明、分级）。
// 运行：cd F:\coding\guanjia\openclaw && npx --yes tsx test\perm-v2-tools.mts
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { classify, type LevelsFile } from "../plugins/guanjia-guard/src/levels";
import { createGuard } from "../plugins/guanjia-guard/src/guard";
import { createStore } from "../plugins/guanjia-guard/src/store";
import { APP_NAME_RE, createGetLocationTool, createGetWeatherTool, createLaunchAppTool, weatherCn, type RunPs } from "../plugins/guanjia-guard/src/ps-tools";

const pluginDir = path.resolve(import.meta.dirname, "../plugins/guanjia-guard");
const levels = JSON.parse(fs.readFileSync(path.join(pluginDir, "defaults/levels.json"), "utf8")) as LevelsFile;
const manifest = JSON.parse(fs.readFileSync(path.join(pluginDir, "openclaw.plugin.json"), "utf8"));
const WS = "C:\\Users\\inksn\\.openclaw\\workspace";

// ---------- 1. 清单与分级 ----------
assert.deepEqual(manifest.contracts.tools, ["speak", "launch_app", "get_location", "get_weather"]);
assert.equal(levels.tools.launch_app, "L1");
assert.equal(levels.tools.get_location, "L0");
assert.equal(levels.tools.get_weather, "L0");
const store = createStore(fs.mkdtempSync(path.join(os.tmpdir(), "gj-v2t-")), path.join(pluginDir, "defaults"));
const g = createGuard(store, levels);
for (const [id, args] of [["openclaw:guanjia-guard:launch_app", { name: "lol" }], ["launch_app", { name: "lol" }], ["openclaw:guanjia-guard:get_location", {}], ["get_weather", { city: "上海" }]] as const) {
  assert.equal(g.beforeToolCall({ toolName: "tool_call", params: { id, args }, toolCallId: `c_${id}` }, { sessionKey: "agent:main:main" }), undefined, `tool_call ${id} 直接放行`);
}
// cron 会话里也直接做（不降档）
g.noteRun("rc", "cron");
assert.equal(g.beforeToolCall({ toolName: "launch_app", params: { name: "lol" } }, { sessionKey: "agent:main:main", runId: "rc" }), undefined);

// ---------- 2. launch_app ----------
const calls: any[] = [];
const fakePs = (stdout: string, code: number | null = 0): RunPs => async (scriptPath, args, timeoutMs) => {
  calls.push({ scriptPath, args, timeoutMs });
  return { stdout, code };
};
const launch = (stdout: string, code: number | null = 0) => createLaunchAppTool({ workspaceDir: WS, runPs: fakePs(stdout, code) }) as any;
const t = launch("");
assert.equal(t.name, "launch_app");
assert.deepEqual(t.parameters.required, ["name"]);
assert.match(t.description, /apps\.json/);
for (const bad of ["", "英雄联盟", "lol; calc", "lol & del x", "../x", "lol\"", "a b", "x".repeat(41), 123]) {
  const before = calls.length;
  const r = await launch('{"ok":true}').execute("c", { name: bad });
  assert.equal(r.details.error, "bad_name", `应拒绝：${JSON.stringify(bad)}`);
  assert.equal(calls.length, before, "参数不合法时不调脚本");
}
for (const okName of ["lol", "bf6", "wall_paper", "epic-games", "LOL"]) assert.ok(APP_NAME_RE.test(okName));
let r = await launch('{"ok":true,"app":"lol","target":"E:\\\\WeGameApps\\\\x.exe"}').execute("c1", { name: " lol " });
assert.equal(r.details.ok, true);
assert.match(r.content[0].text, /已启动 lol/);
assert.equal(calls.at(-1).scriptPath, path.join(WS, "tools", "launch-app.ps1"));
assert.deepEqual(calls.at(-1).args, ["-Name", "lol"]);
assert.equal(calls.at(-1).timeoutMs, 20_000);
r = await launch('{"ok":false,"error":"not_in_map","name":"xyz","known":["lol","steam"]}', 2).execute("c2", { name: "xyz" });
assert.equal(r.details.error, "not_in_map");
assert.match(r.content[0].text, /lol、steam/);
r = await launch('{"ok":false,"error":"no_target","app":"genshin","note":"未安装"}', 3).execute("c3", { name: "genshin" });
assert.match(r.content[0].text, /还没登记路径/);
r = await launch('{"ok":false,"error":"missing_file","app":"steam","target":"F:\\\\steam\\\\steam.exe"}', 4).execute("c4", { name: "steam" });
assert.match(r.content[0].text, /路径失效/);
r = await launch("", null).execute("c5", { name: "lol" });
assert.equal(r.details.error, "no_output");

// ---------- 3. get_location ----------
r = await (createGetLocationTool({ workspaceDir: WS, runPs: fakePs('{"lat":31.23,"lon":121.47,"source":"windows-location","reason":"ok"}') }) as any).execute("c6", {});
assert.equal(r.details.lat, 31.23);
assert.equal(r.details.source, "windows-location");
assert.equal(calls.at(-1).scriptPath, path.join(WS, "tools", "get-location.ps1"));
assert.deepEqual(calls.at(-1).args, [], "定位脚本不带任何参数");
r = await (createGetLocationTool({ workspaceDir: WS, runPs: fakePs("garbage", 1) }) as any).execute("c7", {});
assert.equal(r.details.source, "fallback-beijing", "脚本坏了按北京");

// ---------- 4. get_weather（假 fetch） ----------
const urls: string[] = [];
const forecast = { current: { temperature_2m: 12.4, apparent_temperature: 10.2, weather_code: 0 }, daily: { weather_code: [3, 61], temperature_2m_max: [18.2, 15], temperature_2m_min: [8.1, 9], precipitation_probability_max: [10, 80] } };
const fakeFetch = (geo: unknown) => async (url: string) => {
  urls.push(url);
  return { status: 200, text: async () => JSON.stringify(url.includes("geocoding") ? geo : forecast) };
};
const weather = (geo: unknown, ps = fakePs('{"lat":39.9042,"lon":116.4074,"source":"fallback-beijing"}')) => createGetWeatherTool({ workspaceDir: WS, runPs: ps, fetchImpl: fakeFetch(geo) }) as any;
r = await weather({}).execute("w1", {});
assert.equal(r.details.place, "北京");
assert.equal(r.details.lat, 39.9, "坐标只留两位小数");
assert.equal(r.details.umbrella, true, "明天下雨要带伞");
assert.match(r.content[0].text, /^北京：现在12度（体感10度），晴；今天8到18度，多云/);
assert.match(urls.at(-1)!, /latitude=39\.9&longitude=116\.41/);
r = await weather({}, fakePs('{"lat":31.2345,"lon":121.4789,"source":"windows-location"}')).execute("w2", {});
assert.equal(r.details.place, "家这边");
r = await weather({ results: [{ latitude: 31.23, longitude: 121.47, name: "上海" }] }).execute("w3", { city: "上海" });
assert.equal(r.details.place, "上海");
assert.match(urls.find((u) => u.includes("geocoding"))!, /name=%E4%B8%8A%E6%B5%B7&count=1&language=zh/);
r = await weather({ results: [] }).execute("w4", { city: "不存在市" });
assert.equal(r.details.error, "city_not_found");
r = await (createGetWeatherTool({ workspaceDir: WS, runPs: fakePs("{}"), fetchImpl: async () => { throw new TypeError("fetch failed"); } }) as any).execute("w5", {});
assert.equal(r.details.error, "fetch_failed");
assert.match(r.content[0].text, /get_location 加 web_fetch/);
assert.equal(weatherCn(95), "雷雨");
assert.equal(weatherCn(45), "有雾");

// ---------- 5. 真 execFile：固定参数调 powershell -File，不经 shell ----------
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "gj-v2ps-"));
fs.mkdirSync(path.join(tmp, "tools"));
// 假启动器：原样回显收到的参数个数和 -Name 的值（UTF-8 带 BOM，PowerShell 5.1 才认中文）
const fake = "\uFEFFparam([string]$Name)\n[Console]::OutputEncoding = [Text.Encoding]::UTF8\n@{ ok = $true; app = $Name; argc = $args.Count } | ConvertTo-Json -Compress\n";
fs.writeFileSync(path.join(tmp, "tools", "launch-app.ps1"), fake, "utf8");
r = await (createLaunchAppTool({ workspaceDir: tmp }) as any).execute("real", { name: "lol" });
assert.equal(r.details.ok, true, JSON.stringify(r));
assert.equal(r.details.app, "lol");

console.log("PASS perm v2 tools launch_app / get_location / get_weather / manifest / levels");
