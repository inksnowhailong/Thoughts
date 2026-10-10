import crypto from "node:crypto";
import fs from "node:fs";
import type { AskCategory, LevelOverride } from "./levels";
import type { Store } from "./store";

/**
 * 「以后都允许」：把主人口头同意过的那件事记成一条放行规则，写进线上 levels.json 的 overrides。
 *
 *   只记 exec：精确匹配这条命令（空白多少、斜杠方向不算差别）
 *   结束进程：按程序名记（taskkill /im X、Stop-Process -Name X、Get-Process X | Stop-Process 三种写法都认，不许串别的命令）
 *   读密钥、改管家规则两类在分级表里是 remember: false，调用方不会走到这里
 *
 * 面板（语音进程控制口）只删 source 为 remembered 的条目；双方都是"读最新 → 只改 overrides → 临时文件 → 原子替换"。
 */

/** 一直有效直到在面板里撤销 */
export const REMEMBERED_EXPIRES_AT = "9999-12-31T00:00:00.000Z";

export const CATEGORY_CN: Record<AskCategory, string> = {
  delete: "删除文件",
  system: "影响系统稳定的操作",
  power: "关机或重启",
  kill: "结束进程",
  "download-run": "下载后运行",
  opaque: "看不清内容的命令",
  script: "要执行的内容里有删除、关机这类操作",
  secret: "读取含密钥的配置",
  "self-config": "修改管家自己的规则或工具",
};

export type RememberResult =
  | { ok: true; override: LevelOverride }
  | { ok: false; reason: "not_exec" | "no_command" | "kill_without_name" };

const REGEX_SPECIAL = /[.*+?^${}()|[\]]/;

/** 一条命令 → 只匹配它自己的正则：首尾空白、词间空白多少、斜杠方向不算差别（匹配时不区分大小写） */
export function exactPattern(value: string): string {
  const esc = (ch: string) => (ch === "\\" || ch === "/" ? "[\\\\/]" : REGEX_SPECIAL.test(ch) ? "\\" + ch : ch);
  const body = value.trim().split(/\s+/).map((tok) => [...tok].map(esc).join("")).join("\\s+");
  return `^\\s*${body}\\s*$`;
}

/** 结束进程命令里的程序名（去掉 .exe、转小写）；按 PID 结束、带通配符、取不到时返回 undefined */
export function killTargetName(command: string): string | undefined {
  const res = [
    /\btaskkill(?:\.exe)?\b[^|;&]*?\/im\s+"?([a-z0-9_.-]+)"?/i,
    /\b(?:stop-process|spps|kill)\b[^|;&]*?-(?:name|processname)\s+"?([a-z0-9_.-]+)"?/i,
    /\b(?:get-process|gps|ps)\s+(?:-name\s+)?"?([a-z0-9_.-]+)"?\s*\|\s*(?:stop-process|spps|kill)\b/i,
  ];
  for (const re of res) {
    const m = re.exec(command);
    if (m) return m[1].toLowerCase().replace(/\.exe$/, "");
  }
  return undefined;
}

/** 只匹配"结束某个程序"这一件事的正则（三种常见写法；整条命令必须只做这一件事） */
export function killPattern(name: string): string {
  const n = name.replace(/[.]/g, "\\.");
  return (
    `^\\s*(?:taskkill(?:\\.exe)?(?:\\s+/[ft])*\\s+/im\\s+"?${n}(?:\\.exe)?"?(?:\\s+/[ft])*` +
    `|(?:stop-process|spps|kill)(?:\\s+-force)?\\s+-(?:name|processname)\\s+"?${n}"?(?:\\s+-force)?` +
    `|(?:get-process|gps|ps)\\s+(?:-name\\s+)?"?${n}"?\\s*\\|\\s*(?:stop-process|spps|kill)(?:\\s+-force)?)\\s*$`
  );
}

/**
 * 按这次被问的操作生成一条放行规则（还没写盘）。
 * @param toolName 真实工具名
 * @param params 真实工具参数
 * @param category 先问的类别
 * @param summary 给人看的一行摘要
 */
export function buildRememberedOverride(
  toolName: string,
  params: Record<string, unknown>,
  category: AskCategory | undefined,
  summary: string,
  now = new Date(),
): RememberResult {
  if (toolName !== "exec") return { ok: false, reason: "not_exec" };
  const command = typeof params.command === "string" ? params.command.trim() : "";
  if (!command) return { ok: false, reason: "no_command" };
  let pattern = exactPattern(command);
  let label = summary;
  if (category === "kill") {
    const name = killTargetName(command);
    if (!name) return { ok: false, reason: "kill_without_name" };
    const byName = killPattern(name);
    // 生成的规则必须能匹配这次的原命令，否则退回精确匹配
    if (new RegExp(byName, "i").test(command)) {
      pattern = byName;
      label = `结束进程：${name}`;
    }
  }
  const id = "rem-" + crypto.createHash("sha256").update(`exec\u0000command\u0000${pattern}`).digest("hex").slice(0, 12);
  return {
    ok: true,
    override: {
      id,
      source: "remembered",
      createdAt: now.toISOString(),
      expiresAt: REMEMBERED_EXPIRES_AT,
      scope: "global",
      tool: "exec",
      param: "command",
      pattern,
      level: "L1",
      summary: label,
      reason: "主人口头说以后都允许",
    },
  };
}

/** 一条规则写成一行，与仓库 defaults/levels.json 的排版一致 */
function oneLine(v: unknown): string {
  if (!v || typeof v !== "object" || Array.isArray(v)) return JSON.stringify(v);
  return `{ ${Object.entries(v).map(([k, x]) => `${JSON.stringify(k)}: ${JSON.stringify(x)}`).join(", ")} }`;
}

/** levels.json 的固定排版：顶层每键一行，tools 每个工具一行，rules / overrides 每条一行 */
export function formatLevelsJson(levels: Record<string, unknown>): string {
  const keys = Object.keys(levels);
  const lines = keys.map((k, i) => {
    const v = levels[k];
    let body: string;
    if (Array.isArray(v)) body = v.length === 0 ? "[]" : `[\n${v.map((x) => "    " + oneLine(x)).join(",\n")}\n  ]`;
    else if (v && typeof v === "object") {
      const e = Object.entries(v);
      body = e.length === 0 ? "{}" : `{\n${e.map(([kk, vv]) => `    ${JSON.stringify(kk)}: ${JSON.stringify(vv)}`).join(",\n")}\n  }`;
    } else body = JSON.stringify(v);
    return `  ${JSON.stringify(k)}: ${body}${i < keys.length - 1 ? "," : ""}`;
  });
  return `{\n${lines.join("\n")}\n}\n`;
}

/** Windows 上别的进程正开着文件时 rename 会 EPERM/EBUSY，短暂重试 */
function renameWithRetry(from: string, to: string) {
  for (let i = 0; ; i++) {
    try {
      fs.renameSync(from, to);
      return;
    } catch (err: any) {
      if (i >= 4 || (err?.code !== "EPERM" && err?.code !== "EBUSY" && err?.code !== "EACCES")) throw err;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
    }
  }
}

/**
 * 把放行规则追加进线上 levels.json（读最新 → 只改 overrides → 临时文件 → 原子替换）。
 * 同一条规则已经在了就不重复加。文件读不出来时不写（不能拿默认表覆盖主人手改过的文件）。
 */
export function appendOverride(store: Store, override: LevelOverride): { ok: true; id: string; existed: boolean } | { ok: false; reason: string } {
  const file = store.file("levels.json");
  let levels: Record<string, unknown>;
  try {
    levels = JSON.parse(fs.readFileSync(file, "utf8").replace(/^\uFEFF/, ""));
  } catch (err: any) {
    return { ok: false, reason: `levels_unreadable: ${String(err?.message ?? err)}` };
  }
  const list = (Array.isArray(levels.overrides) ? levels.overrides : []) as LevelOverride[];
  const same = list.find((o) => o.tool === override.tool && o.param === override.param && o.pattern === override.pattern && Date.parse(o.expiresAt) > Date.now());
  if (same) return { ok: true, id: same.id ?? override.id!, existed: true };
  levels.overrides = [...list, override];
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  try {
    fs.writeFileSync(tmp, formatLevelsJson(levels), "utf8");
    renameWithRetry(tmp, file);
  } catch (err: any) {
    try { fs.unlinkSync(tmp); } catch { /* 临时文件可能没建成 */ }
    return { ok: false, reason: `write_failed: ${String(err?.message ?? err)}` };
  }
  return { ok: true, id: override.id!, existed: false };
}
