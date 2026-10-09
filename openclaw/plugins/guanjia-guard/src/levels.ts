/**
 * 行为分级判定（D6 / D8）。纯函数，不做 IO，方便单独测试。
 *
 *   overrides(未过期) → rules(首个命中) → tools(按名) → defaultLevel
 *                                   │
 *                        主动发起(cron/心跳) 再降一档
 *
 * 结构化工具搜索的外层 tool_call{id,args} 先用 resolveToolCallTarget 还原成真实工具，再交给 classify。
 */

export type Level = "L0" | "L1" | "L2" | "L3";

/** 一条按参数匹配的规则；param 为 "$derivedPaths" 时要求宿主推导出的所有路径都命中，为 "$anyDerivedPaths" 时任一路径命中即可 */
export interface LevelRule {
  tool: string;
  param?: string;
  pattern?: string;
  level: Level;
  note?: string;
}

/** D8 临时提权/降权：必须有有效期，可限定作用域（global 或 session:<key>） */
export interface LevelOverride extends LevelRule {
  scope?: string;
  expiresAt: string;
  reason?: string;
}

export interface LevelsFile {
  defaultLevel: Level;
  proactiveDowngrade?: boolean;
  tools: Record<string, Level>;
  rules: LevelRule[];
  overrides?: LevelOverride[];
}

export interface Classification {
  level: Level;
  /** 判定来源，写审计用；client 表示 tool_call 指向 client 来源工具，被强制 L3 */
  source: "override" | "rule" | "tool" | "default" | "client";
  note?: string;
  /** 是否因主动发起被降档 */
  downgraded: boolean;
}

/** tool_call 外层参数还原出的真实调用 */
export interface ToolCallTarget {
  /** 真实工具名，已按 OpenClaw normalizeToolPolicyName 的规则小写并换别名 */
  toolName: string;
  /** 目录来源 openclaw / mcp / client；id 只给了裸工具名时为 undefined */
  source?: string;
  /** 真实工具参数 */
  params: Record<string, unknown>;
}

const ORDER: Level[] = ["L0", "L1", "L2", "L3"];

/** 与 OpenClaw tool-policy-shared 的 TOOL_NAME_ALIASES 保持一致 */
const TOOL_NAME_ALIASES: Record<string, string> = { bash: "exec", "apply-patch": "apply_patch", cron: "automations" };
/** OpenClaw 读取 tool_call 目标的参数名，按优先级 */
const SELECTOR_KEYS = ["id", "toolId", "name"] as const;
/** 目录 id 形如 openclaw:core:exec、mcp:<server>:<tool>、client:client:<tool> */
const CATALOG_ID_RE = /^(openclaw|mcp|client):[^:]*:(.+)$/i;

const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** 主动发起降一档：L0/L1 → +1，L2 → L3，L3 不变 */
export function downgrade(level: Level): Level {
  return ORDER[Math.min(ORDER.indexOf(level) + 1, 3)];
}

/** 判断一条规则是否命中本次工具调用 */
function ruleMatches(rule: LevelRule, toolName: string, params: Record<string, unknown>, derivedPaths?: readonly string[]): boolean {
  if (rule.tool !== toolName) return false;
  if (!rule.pattern) return true;
  const re = new RegExp(rule.pattern, "i");
  if (rule.param === "$derivedPaths" || rule.param === "$anyDerivedPaths") {
    if (!derivedPaths || derivedPaths.length === 0) return false;
    return rule.param === "$derivedPaths" ? derivedPaths.every((p) => re.test(p)) : derivedPaths.some((p) => re.test(p));
  }
  const value = rule.param ? params[rule.param] : JSON.stringify(params);
  return typeof value === "string" && re.test(value);
}

/**
 * 把外层 tool_call 的参数还原成真实工具名与参数（对齐 OpenClaw readToolSearchCallArgs）。
 * @param params tool_call 的原始参数，通常是 { id, args }
 * @returns 没有 id / toolId / name 时返回 undefined，调用方按 tool_call 本身定级
 */
export function resolveToolCallTarget(params: Record<string, unknown>): ToolCallTarget | undefined {
  const key = SELECTOR_KEYS.find((k) => typeof params[k] === "string" && (params[k] as string).trim() !== "");
  if (!key) return undefined;
  // 没有嵌套 args 时 OpenClaw 会挑"在目录里查得到"的那个选择器，插件看不到目录，选择器互相矛盾就无法确定真实目标
  const selectors = new Set(SELECTOR_KEYS.map((k) => params[k]).filter((v): v is string => typeof v === "string" && v.trim() !== "").map((v) => v.trim()));
  if (selectors.size > 1) return { toolName: "tool_call", source: "ambiguous", params };
  const selector = (params[key] as string).trim();
  const m = CATALOG_ID_RE.exec(selector);
  const rawName = (m ? m[2] : selector).trim().toLowerCase();
  const isDotted = (k: string) => k.startsWith("args.") && k.length > 5;
  const dotted = Object.fromEntries(Object.entries(params).filter(([k]) => isDotted(k)).map(([k, v]) => [k.slice(5), v]));
  const nested = params.args ?? params.input;
  let input: Record<string, unknown>;
  if (isRecord(nested) && Object.keys(nested).length > 0) input = { ...dotted, ...nested };
  // 非对象参数不猜，按无参数定级：exec 无 command 时落到 tools.exec 的 L3，更保守
  else if (nested != null && !isRecord(nested)) input = {};
  else input = { ...dotted, ...Object.fromEntries(Object.entries(params).filter(([k]) => k !== "args" && k !== "input" && k !== key && !isDotted(k))) };
  const canonical = TOOL_NAME_ALIASES[rawName] ?? rawName;
  // findEntry 按名字精确匹配、不转小写也不认别名：裸名字和规范名不一致时，插件无法确定 OpenClaw 会匹配到哪个工具
  const source = m ? m[1].toLowerCase() : selector === canonical ? undefined : "ambiguous";
  return { toolName: canonical, source, params: input };
}

/**
 * 给一次工具调用定级。
 * @param levels 分级表（workspace/guanjia/levels.json）
 * @param call 工具名、参数、宿主推导路径、会话 key
 * @param proactive 是否为 cron/心跳等主动发起的会话
 * @param now 当前时间（便于测试注入）
 */
export function classify(
  levels: LevelsFile,
  call: { toolName: string; params: Record<string, unknown>; derivedPaths?: readonly string[]; sessionKey?: string },
  proactive: boolean,
  now = new Date(),
): Classification {
  let result: Omit<Classification, "downgraded"> | undefined;

  // 1) D8 覆盖规则：没有有效期或已过期的一律忽略
  for (const o of levels.overrides ?? []) {
    const exp = Date.parse(o.expiresAt);
    if (!Number.isFinite(exp) || exp <= now.getTime()) continue;
    const inScope = !o.scope || o.scope === "global" || o.scope === `session:${call.sessionKey}`;
    if (inScope && ruleMatches(o, call.toolName, call.params, call.derivedPaths)) {
      result = { level: o.level, source: "override", note: o.reason ?? o.note };
      break;
    }
  }

  // 2) 参数规则 3) 工具名 4) 默认
  if (!result) {
    const rule = levels.rules.find((r) => ruleMatches(r, call.toolName, call.params, call.derivedPaths));
    if (rule) result = { level: rule.level, source: "rule", note: rule.note };
    else if (levels.tools[call.toolName]) result = { level: levels.tools[call.toolName], source: "tool" };
    else result = { level: levels.defaultLevel, source: "default" };
  }

  const shouldDowngrade = proactive && levels.proactiveDowngrade !== false;
  return { ...result, level: shouldDowngrade ? downgrade(result.level) : result.level, downgraded: shouldDowngrade };
}
