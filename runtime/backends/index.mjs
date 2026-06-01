// 思绪运行时 — AgentBackend 适配层（探测 + 分发）
// 统一接口让 daemon 无需关心"用哪个模型怎么跑一轮对话"。
// 每个后端实现：{ name, agentic, isAvailable(), async run({prompt, cwd, timeoutMs}) }
//   · agentic=true  → 后端自带 Read/Write/Bash 工具，潜意识可自行读写记忆文件
//   · agentic=false → 无工具，daemon 需把上下文喂进 prompt 并自行落盘返回结果

import { claudeBackend } from './claude.mjs';
import { cursorBackend } from './cursor.mjs';
import { apiBackend } from './api.mjs';

export { NEEDS_SHELL, commandExists } from './_exec.mjs';

/** 按优先级排列的后端列表 */
const BACKENDS = [claudeBackend, cursorBackend, apiBackend];

/**
 * 解析要使用的后端。
 * @param {string} [preferred] 偏好后端名（'claude'|'cursor'|'api'|'auto'）
 * @returns {object} 命中的后端对象
 * @throws 当没有任何后端可用时抛错
 */
export function resolveBackend(preferred = 'auto') {
    if (preferred && preferred !== 'auto') {
        const picked = BACKENDS.find((b) => b.name === preferred);
        if (!picked) throw new Error(`未知后端: ${preferred}`);
        if (!picked.isAvailable()) throw new Error(`后端 ${preferred} 当前不可用（命令缺失或未配置 API key）`);
        return picked;
    }
    const available = BACKENDS.find((b) => b.isAvailable());
    if (!available) {
        throw new Error('没有可用的 AgentBackend：请安装 claude / cursor-agent CLI，或设置 ANTHROPIC_API_KEY');
    }
    return available;
}

/** 列出所有后端及其可用性，供 status 命令展示 */
export function backendStatus() {
    return BACKENDS.map((b) => ({ name: b.name, agentic: b.agentic, available: b.isAvailable() }));
}
