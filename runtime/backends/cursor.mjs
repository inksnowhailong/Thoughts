// AgentBackend — Cursor Agent（headless）
// 通过 `cursor-agent -p` 跑一轮无界面对话。同样自带工具（agentic=true）。

import { commandExists, runCli } from './_exec.mjs';

export const cursorBackend = {
    name: 'cursor',
    agentic: true,

    isAvailable() {
        return commandExists('cursor-agent');
    },

    /**
     * 跑一轮对话。prompt 通过 stdin 传入。
     * @param {{ prompt: string, cwd?: string, timeoutMs?: number }} opts
     */
    async run({ prompt, cwd, timeoutMs }) {
        return runCli('cursor-agent', ['-p', '--output-format', 'text'], {
            input: prompt, cwd, timeoutMs,
        });
    },
};
