// AgentBackend — Claude Code（headless / print 模式）
// 通过 `claude -p` 跑一轮无界面对话。该后端自带 Read/Write/Bash 工具，
// 因此潜意识可以让它自己读写记忆文件（agentic=true）。

import { commandExists, runCli } from './_exec.mjs';

export const claudeBackend = {
    name: 'claude',
    agentic: true,

    isAvailable() {
        return commandExists('claude');
    },

    /**
     * 跑一轮对话。prompt 通过 stdin 传入。
     * @param {{ prompt: string, cwd?: string, timeoutMs?: number }} opts
     */
    async run({ prompt, cwd, timeoutMs }) {
        return runCli('claude', ['-p', '--output-format', 'text', '--permission-mode', 'acceptEdits'], {
            input: prompt, cwd, timeoutMs,
        });
    },
};
