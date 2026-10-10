import path from "node:path";
import { execFile } from "node:child_process";

/**
 * 常用动作的专用工具，模型不用再拼命令（命令形状一变就对不上分级规则）：
 *
 *   launch_app{name}  ──execFile──> powershell -File workspace\tools\launch-app.ps1 -Name <name>   （L1）
 *   get_location{}    ──execFile──> powershell -File workspace\tools\get-location.ps1              （L0）
 *   get_weather{city?} ─ 没给城市先跑定位脚本 ─> open-meteo 预报（有城市先查 open-meteo 地名）  （L0）
 *
 * 都不经过 shell，脚本路径固定；launch_app 的 name 只许英文字母、数字、下划线、短横。
 * 工具搜索模式下它们是延期工具：模型用 tool_call（id: launch_app / get_location / get_weather）调用，AGENTS.md 里写明了。
 */

export const APP_NAME_RE = /^[A-Za-z0-9_-]{1,40}$/;
export const LAUNCH_TIMEOUT_MS = 20_000;
export const LOCATION_TIMEOUT_MS = 15_000;
export const WEATHER_TIMEOUT_MS = 8_000;
const BEIJING = { lat: 39.9, lon: 116.41 };

export type RunPs = (scriptPath: string, args: string[], timeoutMs: number) => Promise<{ stdout: string; code: number | null }>;
type FetchLike = (url: string, init: { signal: AbortSignal }) => Promise<{ status: number; text(): Promise<string> }>;

export interface PsToolOptions {
  workspaceDir: string;
  runPs?: RunPs;
  fetchImpl?: FetchLike;
}

/** 固定参数调 Windows PowerShell 跑一个脚本文件；退出码非 0 也照样交回 stdout（脚本用退出码区分错误） */
export const defaultRunPs: RunPs = (scriptPath, args, timeoutMs) =>
  new Promise((resolve) => {
    execFile(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", scriptPath, ...args],
      { windowsHide: true, timeout: timeoutMs, encoding: "utf8" },
      (err: any, stdout) => resolve({ stdout: String(stdout ?? ""), code: err ? (typeof err.code === "number" ? err.code : null) : 0 }),
    );
  });

function textResult(text: string, details: Record<string, unknown>) {
  return { content: [{ type: "text", text }], details };
}

/** 脚本只输出一行 JSON；取最后一个能解析的行 */
function lastJson(stdout: string): any {
  const lines = stdout.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      return JSON.parse(lines[i]);
    } catch {
      // 继续往前找
    }
  }
  return undefined;
}

export function createLaunchAppTool(opts: PsToolOptions) {
  const runPs = opts.runPs ?? defaultRunPs;
  const script = path.join(opts.workspaceDir, "tools", "launch-app.ps1");
  return {
    name: "launch_app",
    label: "Launch app",
    description:
      "Launch a game or program registered in apps.json on the home PC (英雄联盟/LOL, Steam, WeGame, 网易云 ...). " +
      "Input: name = the apps.json key in English, e.g. lol, naraka, bf6, steam, wegame, music. " +
      "Returns ok, or the list of known keys when the name is not registered.",
    parameters: {
      type: "object",
      properties: { name: { type: "string", description: "apps.json 里的英文键，例如 lol" } },
      required: ["name"],
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: { name?: unknown }) {
      const name = typeof params?.name === "string" ? params.name.trim() : "";
      if (!APP_NAME_RE.test(name)) {
        return textResult("没有启动：name 只能是 apps.json 里的英文键（字母、数字、下划线、短横），例如 lol。", { ok: false, error: "bad_name" });
      }
      const { stdout, code } = await runPs(script, ["-Name", name], LAUNCH_TIMEOUT_MS);
      const r = lastJson(stdout);
      if (!r) return textResult(`没有启动：启动器没有返回结果（退出码 ${code}）。`, { ok: false, error: "no_output", code });
      if (r.ok) return textResult(`已启动 ${r.app}。`, { ok: true, app: r.app });
      const known = Array.isArray(r.known) ? r.known.join("、") : "";
      const text =
        r.error === "not_in_map" ? `没有启动：apps.json 里没有「${name}」。已登记的有：${known}。`
        : r.error === "no_target" ? `没有启动：${r.app} 还没登记路径（${r.note ?? ""}）。`
        : r.error === "missing_file" ? `没有启动：${r.app} 的路径失效了（${r.target}）。`
        : `没有启动：${r.error ?? "未知错误"}。`;
      return textResult(text, { ok: false, error: r.error ?? "unknown", app: r.app });
    },
  };
}

/** 跑定位脚本；失败时按北京 */
async function locate(runPs: RunPs, script: string): Promise<{ lat: number; lon: number; source: string; reason?: string }> {
  const { stdout } = await runPs(script, [], LOCATION_TIMEOUT_MS);
  const r = lastJson(stdout);
  if (r && Number.isFinite(r.lat) && Number.isFinite(r.lon)) return { lat: r.lat, lon: r.lon, source: String(r.source), reason: r.reason };
  return { ...BEIJING, source: "fallback-beijing", reason: "script-failed" };
}

export function createGetLocationTool(opts: PsToolOptions) {
  const runPs = opts.runPs ?? defaultRunPs;
  const script = path.join(opts.workspaceDir, "tools", "get-location.ps1");
  return {
    name: "get_location",
    label: "Get location",
    description:
      "Get the home PC's current latitude/longitude from the Windows location service (not IP-based, so a VPN does not move it). " +
      "Falls back to Beijing. Use for weather or 'where am I' questions.",
    parameters: { type: "object", properties: {}, additionalProperties: false },
    async execute() {
      const loc = await locate(runPs, script);
      const where = loc.source === "fallback-beijing" ? "没定位到，按北京" : "电脑自己定位到的";
      return textResult(`纬度 ${loc.lat}，经度 ${loc.lon}（${where}）。`, loc);
    },
  };
}

/** WMO 天气代码 → 中文 */
export function weatherCn(code: number): string {
  if (code === 0) return "晴";
  if (code <= 3) return "多云";
  if (code === 45 || code === 48) return "有雾";
  if (code >= 51 && code <= 57) return "毛毛雨";
  if (code >= 61 && code <= 67) return "雨";
  if (code >= 71 && code <= 77) return "雪";
  if (code >= 80 && code <= 82) return "阵雨";
  if (code === 85 || code === 86) return "阵雪";
  if (code >= 95) return "雷雨";
  return "天气不明";
}

const rainy = (code: number) => (code >= 51 && code <= 67) || (code >= 80 && code <= 82) || code >= 95 || (code >= 71 && code <= 77) || code === 85 || code === 86;

export function createGetWeatherTool(opts: PsToolOptions) {
  const runPs = opts.runPs ?? defaultRunPs;
  const doFetch = opts.fetchImpl ?? (globalThis.fetch as unknown as FetchLike);
  const script = path.join(opts.workspaceDir, "tools", "get-location.ps1");
  const getJson = async (url: string) => {
    const res = await doFetch(url, { signal: AbortSignal.timeout(WEATHER_TIMEOUT_MS) });
    if (res.status !== 200) throw new Error(`http_${res.status}`);
    return JSON.parse(await res.text());
  };
  return {
    name: "get_weather",
    label: "Get weather",
    description:
      "Current weather plus today's and tomorrow's forecast (temperature, rain chance, whether to bring an umbrella). " +
      "Input: optional city name (Chinese or pinyin). Without a city it uses the home PC's own location.",
    parameters: {
      type: "object",
      properties: { city: { type: "string", description: "城市名，可省略（省略 = 家这边）" } },
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: { city?: unknown }) {
      const city = typeof params?.city === "string" ? params.city.trim() : "";
      let lat: number, lon: number, place: string;
      try {
        if (city) {
          const g = await getJson(`https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(city)}&count=1&language=zh`);
          const hit = g?.results?.[0];
          if (!hit) return textResult(`没查到「${city}」这个地方，换个写法（例如拼音）再试。`, { ok: false, error: "city_not_found" });
          lat = hit.latitude; lon = hit.longitude; place = hit.name ?? city;
        } else {
          const loc = await locate(runPs, script);
          lat = loc.lat; lon = loc.lon; place = loc.source === "fallback-beijing" ? "北京" : "家这边";
        }
        const la = Math.round(lat * 100) / 100, lo = Math.round(lon * 100) / 100;
        const w = await getJson(
          `https://api.open-meteo.com/v1/forecast?latitude=${la}&longitude=${lo}` +
          "&current=temperature_2m,apparent_temperature,weather_code,precipitation" +
          "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FShanghai&forecast_days=2",
        );
        const d = w.daily, cur = w.current;
        const day = (i: number) => `${Math.round(d.temperature_2m_min[i])}到${Math.round(d.temperature_2m_max[i])}度，${weatherCn(d.weather_code[i])}，降水概率${d.precipitation_probability_max[i]}%`;
        const umbrella = [0, 1].some((i) => rainy(d.weather_code[i]) || d.precipitation_probability_max[i] >= 40);
        const text = `${place}：现在${Math.round(cur.temperature_2m)}度（体感${Math.round(cur.apparent_temperature)}度），${weatherCn(cur.weather_code)}；今天${day(0)}；明天${day(1)}。${umbrella ? "要带伞。" : "不用带伞。"}`;
        return textResult(text, { ok: true, place, lat: la, lon: lo, umbrella });
      } catch (err) {
        return textResult("没查到天气：天气服务连不上。改用 get_location 加 web_fetch 查。", { ok: false, error: "fetch_failed", detail: String(err) });
      }
    },
  };
}
