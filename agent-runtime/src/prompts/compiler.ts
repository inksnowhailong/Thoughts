/**
 * Prompt Compiler（Phase 0 骨架）：按角色把 prompts/<role>.md 编译成 system prompt。
 *
 * 自研最小实现，不依赖 LangChain PromptTemplate：
 * - 模板用 `<!-- section:NAME -->` 标记行分节，顺序由 sections.ts 的 ROLE_SECTIONS 定义；
 * - `{{VAR}}` 占位符必须全部提供，缺失抛错（宁硬勿静默）；
 * - assertBudget 在编译产物上做字符预算守门，超标抛 PromptBudgetError。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { AGENT_RUNTIME_ROOT } from '../tools/paths.js';
import { ROLE_SECTIONS, type Role } from './sections.js';

export type { Role } from './sections.js';

/** 模板里的一节：名字 + 原始模板文本（含 {{VAR}} 占位符）。 */
export interface PromptSection {
  name: string;
  template: string;
}

/** 编译后的一节：名字 + 渲染完成的字符数（预算按它算）。 */
export interface CompiledSection {
  name: string;
  chars: number;
}

export interface CompiledPrompt {
  system: string;
  sections: CompiledSection[];
}

/** 某个 section 超出字符预算时抛出（含 section 名、实际值、预算）。 */
export class PromptBudgetError extends Error {
  constructor(
    readonly section: string,
    readonly actual: number,
    readonly budget: number,
  ) {
    super(`prompt section "${section}" over budget: ${actual} chars > ${budget} chars`);
    this.name = 'PromptBudgetError';
  }
}

export interface CompileOptions {
  /** 模板目录；默认解析到包根 prompts/。测试或部署形态特殊时可显式传入。 */
  promptsDir?: string;
}

const SECTION_RE = /^<!--\s*section:([A-Za-z0-9_-]+)\s*-->$/;
const VAR_RE = /\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}/g;

function defaultPromptsDir(): string {
  // 用 AGENT_RUNTIME_ROOT（向上找 package.json 的稳定特征）定位包根 prompts/，
  // src 与 dist 两种布局下都对（dist/prompts 不存在——模板不进构建产物）
  return path.join(AGENT_RUNTIME_ROOT, 'prompts');
}

const templateCache = new Map<string, string>();

/** 测试或模板热更新时清缓存用。 */
export function clearPromptTemplateCache(): void {
  templateCache.clear();
}

function loadTemplate(file: string): string {
  const hit = templateCache.get(file);
  if (hit !== undefined) return hit;
  let raw: string;
  try {
    raw = readFileSync(file, 'utf8');
  } catch (e) {
    throw new Error(`读不到 prompt 模板 ${file}：${e instanceof Error ? e.message : String(e)}`);
  }
  templateCache.set(file, raw);
  return raw;
}

/** 按标记行把模板切成节；第一个标记之前的内容（文件头注释）忽略。 */
function parseSections(raw: string, source: string): PromptSection[] {
  const out: PromptSection[] = [];
  let current: PromptSection | null = null;
  for (const line of raw.split(/\r?\n/)) {
    const m = SECTION_RE.exec(line.trim());
    if (m) {
      if (current) out.push(current);
      current = { name: m[1], template: '' };
    } else if (current) {
      current.template += (current.template ? '\n' : '') + line;
    }
  }
  if (current) out.push(current);
  const dup = out.find((s, i) => out.findIndex((t) => t.name === s.name) !== i);
  if (dup) throw new Error(`${source} 里 section "${dup.name}" 重复定义`);
  return out;
}

/**
 * 编译一个角色的 system prompt：读模板、按 ROLE_SECTIONS 顺序拼节、替换全部 {{VAR}}。
 * 变量缺失、模板缺节都抛错；sections 返回各节渲染后的字符数，供 assertBudget / prompt-report 用。
 */
export function compileRole(
  role: Role,
  vars: Record<string, string>,
  opts: CompileOptions = {},
): CompiledPrompt {
  const dir = opts.promptsDir ?? defaultPromptsDir();
  const file = path.join(dir, `${role}.md`);
  const byName = new Map(parseSections(loadTemplate(file), file).map((s) => [s.name, s]));

  const names = ROLE_SECTIONS[role];
  const missingSections = names.filter((n) => !byName.has(n));
  if (missingSections.length > 0) {
    throw new Error(`${file} 缺少 section：${missingSections.join(', ')}（需要：${names.join(', ')}）`);
  }

  const missingVars = new Set<string>();
  const rendered: string[] = [];
  const compiled: CompiledSection[] = [];
  for (const name of names) {
    const template = byName.get(name)!.template.trim();
    const text = template.replace(VAR_RE, (whole, v: string) => {
      if (Object.prototype.hasOwnProperty.call(vars, v)) return vars[v];
      missingVars.add(v);
      return whole; // 反正要抛错，留着占位符便于调试
    });
    rendered.push(text);
    compiled.push({ name, chars: text.length });
  }
  if (missingVars.size > 0) {
    throw new Error(`compileRole("${role}") 缺少模板变量：${[...missingVars].sort().join(', ')}`);
  }
  return { system: rendered.join('\n\n'), sections: compiled };
}

/** 逐节检查字符预算；limits 里没有的节名不检查（允许只约束部分节）。 */
export function assertBudget(sections: CompiledSection[], limits: Record<string, number>): void {
  for (const s of sections) {
    const limit = limits[s.name];
    if (limit !== undefined && s.chars > limit) {
      throw new PromptBudgetError(s.name, s.chars, limit);
    }
  }
}
