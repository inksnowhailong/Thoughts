// 思绪运行时 — 跨平台环境感知
// 只采集低敏元数据（不读文件内容、不读剪贴板、不截图），并按 permissions 过滤。
// 每个信号都按 win32 / darwin / linux 分支实现，缺失命令时静默降级为 null。
// 移植并精简自 main 分支 thoughts.mjs 的采集逻辑。

import { spawnSync } from 'node:child_process';
import { platform } from 'node:os';

/** 统一的子进程执行封装 */
function run(command, args, timeout = 5000) {
    const r = spawnSync(command, args, {
        encoding: 'utf8', windowsHide: true, timeout,
    });
    return {
        ok: (r.status ?? 1) === 0,
        stdout: String(r.stdout ?? '').trim(),
    };
}

/** Windows PowerShell 执行封装 */
function powershell(script, timeout = 5000) {
    return run('powershell', ['-NoProfile', '-Command', script], timeout);
}

const os = platform();

/** Git 仓库状态（短格式 + 最近 3 条提交） */
function senseGit(cwd) {
    const status = spawnSync('git', ['status', '--short'], {
        encoding: 'utf8', windowsHide: true, timeout: 5000, cwd,
    });
    if ((status.status ?? 1) !== 0) return null;
    const log = spawnSync('git', ['log', '--oneline', '-3'], {
        encoding: 'utf8', windowsHide: true, timeout: 5000, cwd,
    });
    return {
        dirty: String(status.stdout ?? '').trim().slice(0, 2000),
        recent: String(log.stdout ?? '').trim(),
    };
}

/** 系统状态：内存 / 运行时间 */
function senseSystem() {
    if (os === 'win32') {
        const r = powershell('Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory,TotalVisibleMemorySize,LastBootUpTime | ConvertTo-Json -Compress');
        return r.ok ? r.stdout.slice(0, 2000) : null;
    }
    const r = run('sh', ['-lc', 'uptime; df -h . | tail -1']);
    return r.stdout.slice(0, 2000) || null;
}

/** 本地监听端口（开发服务器） */
function senseDevServers() {
    if (os === 'win32') {
        const r = powershell('Get-NetTCPConnection -State Listen | Select-Object -First 20 LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress');
        return r.ok ? r.stdout.slice(0, 4000) : null;
    }
    const r = run('sh', ['-lc', 'command -v lsof >/dev/null 2>&1 && lsof -iTCP -sTCP:LISTEN -P | head -20 || true']);
    return r.stdout.slice(0, 4000) || null;
}

/** 当前正在播放的音乐 */
function senseNowPlaying() {
    if (os === 'darwin') {
        const r = run('osascript', ['-e', 'tell application "Music" to get {name, artist} of current track']);
        if (r.ok && r.stdout) return r.stdout;
        const sp = run('osascript', ['-e', 'tell application "Spotify" to get {name of current track, artist of current track}']);
        return sp.ok ? sp.stdout : null;
    }
    if (os === 'win32') {
        // Windows 无统一 API，尝试通过窗口标题粗略推断（缺失则 null）
        const r = powershell('(Get-Process | Where-Object { $_.MainWindowTitle -match " - " -and $_.ProcessName -match "Spotify|Music|foobar" } | Select-Object -First 1 -ExpandProperty MainWindowTitle)');
        return r.ok && r.stdout ? r.stdout : null;
    }
    const r = run('sh', ['-lc', 'command -v playerctl >/dev/null 2>&1 && playerctl metadata --format "{{ title }} - {{ artist }}" 2>/dev/null || true']);
    return r.stdout || null;
}

/** 电池状态 */
function senseBattery() {
    if (os === 'win32') {
        const r = powershell('(Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus | ConvertTo-Json -Compress)');
        return r.ok ? r.stdout : null;
    }
    if (os === 'darwin') {
        const r = run('pmset', ['-g', 'batt']);
        return r.ok ? r.stdout : null;
    }
    const r = run('sh', ['-lc', 'command -v acpi >/dev/null 2>&1 && acpi -b 2>/dev/null || true']);
    return r.stdout || null;
}

/**
 * 各信号的采集器表。key 与 permissions.json 中的权限名一一对应。
 */
const COLLECTORS = {
    git_status: (ctx) => senseGit(ctx.cwd),
    system_info: () => senseSystem(),
    dev_servers: () => senseDevServers(),
    now_playing: () => senseNowPlaying(),
    battery: () => senseBattery(),
};

/**
 * 按权限采集环境快照。
 * @param {Record<string,string>} permissions 权限表（值为 'always' 才采集）
 * @param {{cwd?: string}} ctx 上下文（如当前项目目录）
 * @returns {object} 仅包含已授权且采集成功的信号
 */
export function senseEnvironment(permissions = {}, ctx = {}) {
    const now = new Date();
    const snapshot = {
        capturedAt: now.toISOString(),
        localTime: now.toLocaleString(),
        hour: now.getHours(),
        weekday: now.toLocaleDateString(undefined, { weekday: 'long' }),
    };
    for (const [signal, collect] of Object.entries(COLLECTORS)) {
        if (permissions[signal] !== 'always') continue;
        try {
            const value = collect(ctx);
            if (value != null && value !== '') snapshot[signal] = value;
        } catch {
            // 单个信号失败不影响整体
        }
    }
    return snapshot;
}
