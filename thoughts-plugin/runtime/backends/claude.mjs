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
     * @param {{ prompt: string, cwd?: string, timeoutMs?: number, model?: string }} opts
     *   model 可选：决策层用它指定便宜模型（如 claude-haiku-4-5），省 token。
     */
    async run({
        prompt, cwd, timeoutMs, model,
    }) {
        const args = ['-p', '--output-format', 'text', '--permission-mode', 'acceptEdits'];
        if (model) args.push('--model', model);
        return runCli('claude', args, { input: prompt, cwd, timeoutMs });
    },
};
