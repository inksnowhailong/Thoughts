import { describe, expect, it } from 'vitest';
import {
  assertBudget,
  clearPromptTemplateCache,
  compileRole,
  type CompiledSection,
  PromptBudgetError,
} from '../../src/prompts/compiler.js';
import { DEFAULT_BUDGETS, ROLE_SECTIONS, ROLE_VARS, type Role } from '../../src/prompts/sections.js';

function fullVars(role: Role): Record<string, string> {
  return Object.fromEntries(ROLE_VARS[role].map((k) => [k, `«${k}样例»`]));
}

describe('compileRole', () => {
  it('编译 main：全部 section 按序输出、变量替换、无残留占位符', () => {
    const { system, sections } = compileRole('main', fullVars('main'));
    expect(sections.map((s) => s.name)).toEqual([...ROLE_SECTIONS.main]);
    expect(system).not.toMatch(/\{\{/);
    for (const k of ROLE_VARS.main) {
      expect(system).toContain(`«${k}样例»`);
    }
  });

  it('编译 worker：goal/材料/验收占位符替换', () => {
    const { system, sections } = compileRole('worker', fullVars('worker'));
    expect(sections.map((s) => s.name)).toEqual([...ROLE_SECTIONS.worker]);
    expect(system).toContain('«GOAL样例»');
    expect(system).toContain('«MATERIALS样例»');
    expect(system).toContain('«ACCEPTANCE样例»');
  });

  it('缺变量抛错并列出名字', () => {
    expect(() => compileRole('main', {})).toThrow(/SPEAKER/);
    expect(() => compileRole('worker', { GOAL: 'x', ACCEPTANCE: 'y' })).toThrow(/MATERIALS/);
  });

  it('从包根 prompts/main.md 实际编译：system 非空且 sections 数 > 2', () => {
    clearPromptTemplateCache(); // 确认真的从盘上读，而不是缓存假象
    const { system, sections } = compileRole('main', {
      SPEAKER: '主人',
      CHANNEL: 'voice',
      CONTEXT: '[语音 2026-11-05 10:00 周四 主人] 今天天气怎么样',
      MEMORY: '（暂无记忆）',
    });
    expect(system.trim().length).toBeGreaterThan(0);
    expect(sections.length).toBeGreaterThan(2);
    expect(system).toContain('今天天气'); // {{CONTEXT}} 已渲染进 system
  });

  it('默认预算下 main 实际编译不超支', () => {
    const { sections } = compileRole('main', {
      SPEAKER: '主人',
      CHANNEL: 'voice',
      CONTEXT: '近况。'.repeat(80),
      MEMORY: '记忆。'.repeat(80),
    });
    expect(() => assertBudget(sections, DEFAULT_BUDGETS.main)).not.toThrow();
  });
});

describe('assertBudget', () => {
  const sections: CompiledSection[] = [
    { name: 'persona', chars: 100 },
    { name: 'voice-rules', chars: 2400 },
  ];

  it('预算内通过（含未设限的节）', () => {
    expect(() => assertBudget(sections, { 'voice-rules': 2400, 'context': 1200 })).not.toThrow();
  });

  it('超预算抛 PromptBudgetError（含节名、实际值、预算）', () => {
    const over = [...sections, { name: 'voice-rules', chars: 2401 } as CompiledSection];
    const fn = () => assertBudget(over, { 'voice-rules': 2400 });
    expect(fn).toThrow(PromptBudgetError);
    try {
      fn();
    } catch (e) {
      const err = e as PromptBudgetError;
      expect(err.section).toBe('voice-rules');
      expect(err.actual).toBe(2401);
      expect(err.budget).toBe(2400);
      expect(err.message).toContain('voice-rules');
    }
  });
});
