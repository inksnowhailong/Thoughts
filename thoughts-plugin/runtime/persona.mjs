// 思绪运行时 — 人格渲染（chat 注入 hook 与 daemon 主动循环共用的单一真相）

/**
 * 渲染人格供注入。优先用散文体 persona.md——一段"我是谁、我对你是什么"的连续叙述，
 * 模型拿到的是一个已经活在角色里的人，而非一份待复述的形容词清单（清单最省力的交差方式
 * 就是把它念回来，沦为自报设定）。persona.md 缺失时降级到旧 JSON 字段拼装，保持向后兼容。
 * @param {object} p 人格机器配置（兜底渲染用）
 * @param {string} [personaProse] persona.md 全文（散文体人格画像）
 */
export function renderPersona(p, personaProse = '') {
    const prose = String(personaProse || '').trim();
    if (prose) return prose;
    // —— 兜底：老实例没有 persona.md 时，仍能从 JSON 字段拼出可用人格 ——
    const join = (arr) => (Array.isArray(arr) ? arr.join('；') : '');
    const lines = [`你是${p.name}。${p.essence || ''}`];
    if (p.tone) lines.push(`你说话的调子：${p.tone}。`);
    if (join(p.traits)) lines.push(`你这个人：${join(p.traits)}。`);
    if (p.dynamic) lines.push(`你不套模板——${p.dynamic}`);
    if (join(p.quirks)) lines.push(`你的习惯：${join(p.quirks)}。`);
    if (join(p.boundaries)) lines.push(`你守的边界：${join(p.boundaries)}。`);
    return lines.join('\n');
}
