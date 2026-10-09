/**
 * 行为分级判定（D6 / D8）。纯函数，不做 IO，方便单独测试。
 *
 *   overrides(未过期) → rules(首个命中) → tools(按名) → defaultLevel
 *                                   │
 *                        主动发起(cron/心跳) 再降一档
 */

export type Level = "L0" | "L1" | "L2" | "L3";

/** 一条按参数匹配的规则；param 为 "$derivedPaths" 时要求宿主推导出的所有路径都命中 */
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
  /** 判定来源，写审计用 */
  source: "override" | "rule" | "tool" | "default";
  note?: string;
  /** 是否因主动发起被降档 */
  downgraded: boolean;
}

const ORDER: Level[] = ["L0", "L1", "L2", "L3"];

/** 主动发起降一档：L0/L1 → +1，L2 → L3，L3 不变 */
export function downgrade(level: Level): Level {
  return ORDER[Math.min(ORDER.indexOf(level) + 1, 3)];
}

/** 判断一条规则是否命中本次工具调用 */
function ruleMatches(rule: LevelRule, toolName: string, params: Record<string, unknown>, derivedPaths?: readonly string[]): boolean {
  if (rule.tool !== toolName) return false;
  if (!rule.pattern) return true;
  const re = new RegExp(rule.pattern, "i");
  if (rule.param === "$derivedPaths") {
    return !!derivedPaths && derivedPaths.length > 0 && derivedPaths.every((p) => re.test(p));
  }
  const value = rule.param ? params[rule.param] : JSON.stringify(params);
  return typeof value === "string" && re.test(value);
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
