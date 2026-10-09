// 天气雨雪规则骨架（6b）：command 型 cron 每天早上跑一次。
// 没配 key 就跳过并记审计；配了就查明天有无雨雪，结果写 workspace/guanjia/weather.json，
// 由后续（S1 出声 / 首次开口提示）消费。本脚本不调用模型、不出声。
//
// 环境变量（需在网关进程可见，改完要重启网关）：
//   GUANJIA_WEATHER_API_KEY   必填，没有则跳过
//   GUANJIA_WEATHER_LOCATION  城市/经纬度，默认 101010100（和风天气城市 id 示例，需按实际改）
//   GUANJIA_WEATHER_URL       可选，URL 模板，{key} {location} 会被替换；默认和风天气 3 日预报
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const workspace = process.env.GUANJIA_WORKSPACE ?? path.join(os.homedir(), ".openclaw", "workspace");
const gdir = path.join(workspace, "guanjia");
fs.mkdirSync(gdir, { recursive: true });
const audit = (entry) =>
  fs.appendFileSync(path.join(gdir, "audit.jsonl"), JSON.stringify({ ts: new Date().toISOString(), kind: "weather", ...entry }) + "\n", "utf8");

const key = process.env.GUANJIA_WEATHER_API_KEY;
if (!key) {
  audit({ outcome: "skipped", reason: "GUANJIA_WEATHER_API_KEY not set" });
  console.log("weather-check: skipped (no key)");
  process.exit(0);
}

const location = process.env.GUANJIA_WEATHER_LOCATION ?? "101010100";
const url = (process.env.GUANJIA_WEATHER_URL ?? "https://devapi.qweather.com/v7/weather/3d?location={location}&key={key}")
  .replace("{key}", encodeURIComponent(key))
  .replace("{location}", encodeURIComponent(location));

try {
  const res = await fetch(url, { signal: AbortSignal.timeout(15000) });
  const body = await res.json();
  // 和风天气：daily[1] 为明天；其他服务商通过 GUANJIA_WEATHER_URL 接入时退化为全文关键字判断
  const tomorrow = body?.daily?.[1];
  const text = tomorrow ? `${tomorrow.textDay}/${tomorrow.textNight}` : JSON.stringify(body).slice(0, 2000);
  const rainOrSnow = /雨|雪|rain|snow|sleet/i.test(text);
  const result = { date: tomorrow?.fxDate ?? null, rainOrSnow, summary: tomorrow ? text : null, checkedAt: new Date().toISOString() };
  fs.writeFileSync(path.join(gdir, "weather.json"), JSON.stringify(result, null, 2), "utf8");
  audit({ outcome: "ok", status: res.status, rainOrSnow });
  console.log(`weather-check: rainOrSnow=${rainOrSnow}`);
} catch (err) {
  audit({ outcome: "error", error: String(err?.message ?? err) });
  console.log(`weather-check: error ${err?.message ?? err}`);
}
