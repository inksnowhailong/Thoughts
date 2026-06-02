// 思绪运行时 — 跨平台系统通知（全部走系统原生，零模块依赖）
// Windows: WinRT toast + 自注册专属 AppId（不依赖 BurntToast，规避借 PowerShell 身份被关的坑）
// macOS:   terminal-notifier（装了则用，身份独立更可靠）→ 否则降级原生 osascript
// Linux:   notify-send → 否则降级终端打印

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
        // Windows 原生 WinRT toast + 自注册专属 AppId（零模块依赖，合思绪零依赖底线）。
        // 为何不用 BurntToast：它借 PowerShell 的通知身份，一旦该身份被单独关掉就静默失败；
        // 自注册一个 AppId 让思绪有独立通知身份，新身份默认开启，规避此坑。
        const xmlEscape = (str) => String(str)
            .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;').replaceAll("'", '&apos;');
        const line1 = xmlEscape(`${safeTitle} ${safeSubtitle}`.trim());
        const line2 = xmlEscape(safeMessage);
        const toastXml = `<toast><visual><binding template="ToastGeneric"><text>${line1}</text><text>${line2}</text></binding></visual></toast>`;
        executable = 'powershell';
        args = ['-NoProfile', '-Command', [
            "$ErrorActionPreference='Stop'",
            "$a='Thoughts.Companion'",
            '$r="HKCU:\\SOFTWARE\\Classes\\AppUserModelId\\$a"',
            'if(-not(Test-Path $r)){New-Item -Path $r -Force|Out-Null}',
            "New-ItemProperty -Path $r -Name 'DisplayName' -Value '思绪' -PropertyType String -Force|Out-Null",
            '[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]',
            '[void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime]',
            '$x=New-Object Windows.Data.Xml.Dom.XmlDocument',
            `$x.LoadXml('${toastXml}')`,
            '$t=New-Object Windows.UI.Notifications.ToastNotification $x',
            '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($a).Show($t)',
        ].join('; ')];
    } else if (os === 'darwin') {
        // 优先 terminal-notifier：它有独立通知身份，比寄生在终端/Script Editor 权限上的
        // osascript 更可靠（osascript 在宿主通知权限被关或勿扰时会静默失败）。两者都按数组传参，免转义。
        const hasTN = (spawnSync('which', ['terminal-notifier'], { encoding: 'utf8' }).status ?? 1) === 0;
        if (hasTN) {
            executable = 'terminal-notifier';
            args = ['-title', safeTitle, '-subtitle', safeSubtitle, '-message', safeMessage];
        } else {
            const t = safeTitle.replaceAll('"', '\\"');
            const s = safeSubtitle.replaceAll('"', '\\"');
            const m = safeMessage.replaceAll('"', '\\"');
            executable = 'osascript';
            args = ['-e', `display notification "${m}" with title "${t}" subtitle "${s}"`];
        }
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
