/**
 * Prompt Compiler 的角色分节定义（Phase 0 骨架）。
 *
 * section 名与 .trae/documents/agents-md-decomposition.md 的拆解映射表一一对应；
 * 模板文件在包根 prompts/<role>.md，用 `<!-- section:NAME -->` 标记行分节，
 * 编译器（src/prompts/compiler.ts）按这里的顺序拼接。
 */

export type Role = 'main' | 'worker';

/** 各角色 system prompt 的 section 顺序。 */
export const ROLE_SECTIONS: Record<Role, readonly string[]> = {
  main: ['platform-min', 'persona', 'voice-rules', 'context', 'memory'],
  worker: ['platform-min', 'goal', 'materials', 'acceptance', 'tool-constraints', 'report'],
};

/** 各角色模板用到的 {{VAR}} 占位符（缺一个 compileRole 就抛错）。 */
export const ROLE_VARS: Record<Role, readonly string[]> = {
  main: ['SPEAKER', 'CHANNEL', 'CONTEXT', 'MEMORY'],
  worker: ['GOAL', 'MATERIALS', 'ACCEPTANCE'],
};

/**
 * 各 section 编译后的字符预算（assertBudget 的默认 limits）。
 *
 * 口径：按“中文 2 字符 ≈ 1 token、英文 4 字符 ≈ 1 token”的保守混合估算，
 * 即 1 token ≈ 2 字符；glm 系分词实际更省，预算即上限。
 * main 成品模板（D1-D6 决议收敛后）固定文本约 900 字符 ≈ 450 token，
 * 加上动态 context/memory 与 Phase 2 工具 schema 后总预算仍 <5k token（总字符 ≤8000）。
 * worker 文本合计 6500 字符 ≈ 3250 token，加任务相关工具子集后 ≤4k token。
 */
export const DEFAULT_BUDGETS: Record<Role, Readonly<Record<string, number>>> = {
  main: {
    'platform-min': 500,
    persona: 300,
    'voice-rules': 900,
    context: 1200,
    memory: 1600,
  },
  worker: {
    'platform-min': 600,
    goal: 1200,
    materials: 2000,
    acceptance: 1200,
    'tool-constraints': 900,
    report: 600,
  },
};
