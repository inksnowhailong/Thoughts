// 思绪运行时 — 后端共享的子进程工具
// 单独成文件，避免 index.mjs 与各后端互相 import 造成循环依赖。

import { spawnSync } from 'node:child_process';
import { platform } from 'node:os';

/** Windows 下 spawn .cmd/.bat 需要 shell:true */
export const NEEDS_SHELL = platform() === 'win32';

/**
 * 检测命令是否可执行（--version 探测）。
 * @param {string} command
 */
export function commandExists(command) {
    try {
        const r = spawnSync(command, ['--version'], {
            encoding: 'utf8', windowsHide: true, timeout: 8000, shell: NEEDS_SHELL,
        });
        return !r.error && (r.status ?? 1) === 0;
    } catch {
        return false;
    }
}

/**
 * 调用一个 CLI 并把 prompt 通过 stdin 传入（避免命令行参数转义问题）。
 * @param {string} command 命令名
 * @param {string[]} args 静态参数（只放 flag，不放 prompt）
 * @param {{ input: string, cwd?: string, timeoutMs?: number }} options
 * @returns {{ ok: boolean, text: string, error?: string }}
 */
export function runCli(command, args, { input, cwd, timeoutMs = 180000 }) {
    const r = spawnSync(command, args, {
        encoding: 'utf8',
        windowsHide: true,
        shell: NEEDS_SHELL,
        cwd,
        timeout: timeoutMs,
        input,
        maxBuffer: 32 * 1024 * 1024,
    });
    if (r.error) return { ok: false, text: '', error: String(r.error.message || r.error) };
    const text = String(r.stdout ?? '').trim();
    return { ok: (r.status ?? 1) === 0, text, error: r.stderr ? String(r.stderr).trim() : undefined };
}
