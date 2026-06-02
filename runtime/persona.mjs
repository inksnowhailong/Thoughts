// 思绪运行时 — 人格渲染（chat 注入 hook 与 daemon 主动循环共用的单一真相）

/**
 * 把人格 JSON 渲染成第二人称浸入式叙述——而不是原样丢一张属性清单。
 * 根因：把 traits/tone/quirks 这种形容词表塞给模型，它最省力的交差方式就是把表念回来
 * （自报设定、「我不会嘘寒问暖」之类）。解法是从形态上断掉诱因——把字段拼成"你是谁、你怎么说话"
 * 的连续话语，让模型拿到的是一个"已经在角色里的人"，而非一份待复述的说明书。
 */
export function renderPersona(p) {
    const join = (arr) => (Array.isArray(arr) ? arr.join('；') : '');
    const lines = [`你是${p.name}。${p.essence || ''}`];
    if (p.tone) lines.push(`你说话的调子：${p.tone}。`);
    if (join(p.traits)) lines.push(`你这个人：${join(p.traits)}。`);
    if (p.dynamic) lines.push(`你不套模板——${p.dynamic}`);
    if (join(p.quirks)) lines.push(`你的习惯：${join(p.quirks)}。`);
    if (join(p.boundaries)) lines.push(`你守的边界：${join(p.boundaries)}。`);
    return lines.join('\n');
}
