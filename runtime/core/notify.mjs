// 思绪运行时 — 跨平台系统通知
// Windows: BurntToast（缺失则降级到终端输出）
// macOS:   osascript display notification
// Linux:   notify-send（缺失则降级到终端输出）
// 移植自 main 分支 thoughts.mjs 的 notify 实现。

import { spawnSync } from 'node:child_process';
import { platform } from 'node:os';

/**
 * 发送一条系统通知。
 * @param {string} title 标题（通常为人格名称）
 * @param {string} subtitle 副标题（通常放一个颜文字）
 * @param {string} message 正文（完整聊天内容）
 */
export function notify(title, subtitle, message) {
    const safeTitle = String(title ?? '思绪');
    const safeSubtitle = String(subtitle ?? '');
    const safeMessage = String(message ?? '');
    const os = platform();

    let executable;
    let args;

    if (os === 'win32') {
        const t = safeTitle.replaceAll("'", "''");
        const s = safeSubtitle.replaceAll("'", "''");
        const m = safeMessage.replaceAll("'", "''");
        executable = 'powershell';
        args = ['-NoProfile', '-Command', [
            "$ErrorActionPreference = 'SilentlyContinue'",
            'if (Get-Module -ListAvailable -Name BurntToast) {',
            `  New-BurntToastNotification -Text '${t}', '${s}', '${m}' | Out-Null`,
            '} else {',
            `  Write-Output '[思绪通知] ${t} ${s} ${m}'`,
            '}',
        ].join('; ')];
    } else if (os === 'darwin') {
        const t = safeTitle.replaceAll('"', '\\"');
        const s = safeSubtitle.replaceAll('"', '\\"');
        const m = safeMessage.replaceAll('"', '\\"');
        executable = 'osascript';
        args = ['-e', `display notification "${m}" with title "${t}" subtitle "${s}"`];
    } else {
        const t = safeTitle.replaceAll('"', '\\"');
        const body = `${safeSubtitle} ${safeMessage}`.replaceAll('"', '\\"');
        executable = 'sh';
        args = ['-lc', [
            'if command -v notify-send >/dev/null 2>&1; then',
            `notify-send "${t}" "${body}";`,
            'else',
            `printf '%s\\n' "[思绪通知] ${t} ${body}";`,
            'fi',
        ].join(' ')];
    }

    const result = spawnSync(executable, args, { encoding: 'utf8', windowsHide: true });
    return { ok: (result.status ?? 1) === 0, stdout: result.stdout, stderr: result.stderr };
}
