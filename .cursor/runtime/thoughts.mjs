#!/usr/bin/env node
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const ROOT = join(homedir(), '.cursor', '.thoughts');
const ACTIVE_FILE = join(ROOT, 'active.json');
const SESSIONS_FILE = join(ROOT, 'sessions.json');

function ensureDir(path) {
    mkdirSync(path, { recursive: true });
}

function readJson(path, fallback) {
    try {
        return JSON.parse(readFileSync(path, 'utf8'));
    } catch {
        return fallback;
    }
}

function writeJson(path, value) {
    ensureDir(dirname(path));
    writeFileSync(path, `${JSON.stringify(value, null, 4)}\n`, 'utf8');
}

function normalizeWorkspace(value) {
    return resolve(value || process.cwd()).replaceAll('\\', '/');
}

function activeState() {
    return readJson(ACTIVE_FILE, {});
}

function saveActive(state) {
    writeJson(ACTIVE_FILE, state);
}

function instanceDir(name) {
    return join(ROOT, 'instances', name);
}

function latestSession(workspace) {
    const sessions = readJson(SESSIONS_FILE, {});
    return sessions[workspace] ?? null;
}

function listInstances() {
    const root = join(ROOT, 'instances');
    ensureDir(root);
    const items = readdirSync(root, { withFileTypes: true })
        .filter((item) => item.isDirectory())
        .map((item) => item.name)
        .sort((a, b) => a.localeCompare(b));
    console.log(JSON.stringify(items, null, 4));
}

function bind(instanceName, workspaceArg) {
    const workspace = normalizeWorkspace(workspaceArg);
    const session = latestSession(workspace);

    if (!session?.conversation_id) {
        throw new Error(`没有找到 workspace ${workspace} 的最新 Cursor session。请在一个新 chat 中运行 /thoughts。`);
    }

    const state = activeState();
    const dir = instanceDir(instanceName);
    ensureDir(dir);

    const now = Date.now();
    state[workspace] = {
        instance: instanceName,
        conversation_id: session.conversation_id,
        transcript_path: session.transcript_path ?? null,
        enabled: true,
        next_active_at: now,
        updated_at: new Date(now).toISOString(),
    };
    saveActive(state);
    console.log(JSON.stringify(state[workspace], null, 4));
}

function unbind(workspaceArg) {
    const workspace = normalizeWorkspace(workspaceArg);
    const state = activeState();
    const removed = state[workspace] ?? null;
    delete state[workspace];
    saveActive(state);
    console.log(JSON.stringify({ removed }, null, 4));
}

function schedule(workspaceArg, delayMsArg, reason = 'scheduled') {
    const workspace = normalizeWorkspace(workspaceArg);
    const delayMs = Math.max(0, Number(delayMsArg || 0));
    const state = activeState();
    const entry = state[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const nextActiveAt = Date.now() + delayMs;
    entry.next_active_at = nextActiveAt;
    entry.next_active_at_iso = new Date(nextActiveAt).toISOString();
    entry.last_schedule_reason = reason;
    entry.updated_at = new Date().toISOString();
    state[workspace] = entry;
    saveActive(state);
    console.log(JSON.stringify(entry, null, 4));
}

function showState(workspaceArg) {
    const workspace = normalizeWorkspace(workspaceArg);
    console.log(JSON.stringify(activeState()[workspace] ?? null, null, 4));
}

function ensureInstanceFiles(instanceName) {
    const dir = instanceDir(instanceName);
    ensureDir(dir);

    const defaults = {
        'memory-raw.md': '# 思绪记忆 - 原始\n\n',
        'memory-consolidated.md': '# 思绪记忆 - 整理\n\n',
        'activity-log.jsonl': '',
    };

    for (const [file, content] of Object.entries(defaults)) {
        const path = join(dir, file);
        if (!existsSync(path)) writeFileSync(path, content, 'utf8');
    }

    const loopStatePath = join(dir, 'loop-state.json');
    if (!existsSync(loopStatePath)) {
        writeJson(loopStatePath, {
            last_subconscious_at: null,
            consecutive_ignored: 0,
            last_user_message_at: null,
            last_active_message_at: null,
        });
    }

    console.log(dir);
}

function notify(title, subtitle, message) {
    const safeTitle = String(title ?? '思绪');
    const safeSubtitle = String(subtitle ?? '');
    const safeMessage = String(message ?? '');
    const currentPlatform = platform();

    let executable;
    let args;

    if (currentPlatform === 'win32') {
        const escapedTitle = safeTitle.replaceAll("'", "''");
        const escapedSubtitle = safeSubtitle.replaceAll("'", "''");
        const escapedMessage = safeMessage.replaceAll("'", "''");
        executable = 'powershell';
        args = ['-NoProfile', '-Command', [
            "$ErrorActionPreference = 'SilentlyContinue'",
            "if (Get-Module -ListAvailable -Name BurntToast) {",
            `  New-BurntToastNotification -Text '${escapedTitle}', '${escapedSubtitle}', '${escapedMessage}' | Out-Null`,
            '} else {',
            `  Write-Output '[thoughts notification] ${escapedTitle} ${escapedSubtitle} ${escapedMessage}'`,
            '}',
        ].join('; ')];
    } else if (currentPlatform === 'darwin') {
        const escapedTitle = safeTitle.replaceAll('"', '\\"');
        const escapedSubtitle = safeSubtitle.replaceAll('"', '\\"');
        const escapedMessage = safeMessage.replaceAll('"', '\\"');
        executable = 'osascript';
        args = ['-e', `display notification "${escapedMessage}" with title "${escapedTitle}" subtitle "${escapedSubtitle}"`];
    } else {
        executable = 'sh';
        args = ['-lc', [
            'if command -v notify-send >/dev/null 2>&1; then',
            `notify-send "${safeTitle.replaceAll('"', '\\"')}" "${`${safeSubtitle} ${safeMessage}`.replaceAll('"', '\\"')}";`,
            'else',
            `printf '%s\\n' "[thoughts notification] ${safeTitle} ${safeSubtitle} ${safeMessage}";`,
            'fi',
        ].join(' ')];
    }

    const result = spawnSync(executable, args, {
        encoding: 'utf8',
        windowsHide: true,
    });

    if (result.stdout) process.stdout.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
    process.exitCode = result.status ?? 0;
}

const [cmd, ...args] = process.argv.slice(2);

ensureDir(ROOT);

try {
    switch (cmd) {
        case 'root':
            console.log(ROOT);
            break;
        case 'list-instances':
            listInstances();
            break;
        case 'instance-dir':
            console.log(instanceDir(args[0]));
            break;
        case 'ensure-instance':
            ensureInstanceFiles(args[0]);
            break;
        case 'bind':
            bind(args[0], args[1]);
            break;
        case 'unbind':
            unbind(args[0]);
            break;
        case 'schedule':
            schedule(args[0], args[1], args.slice(2).join(' '));
            break;
        case 'state':
            showState(args[0]);
            break;
        case 'notify':
            notify(args[0], args[1], args.slice(2).join(' '));
            break;
        default:
            console.log(`Usage:
  node .cursor/runtime/thoughts.mjs root
  node .cursor/runtime/thoughts.mjs list-instances
  node .cursor/runtime/thoughts.mjs ensure-instance <name>
  node .cursor/runtime/thoughts.mjs bind <instance> [workspace]
  node .cursor/runtime/thoughts.mjs unbind [workspace]
  node .cursor/runtime/thoughts.mjs schedule [workspace] <delayMs> [reason]
  node .cursor/runtime/thoughts.mjs state [workspace]
  node .cursor/runtime/thoughts.mjs notify <title> <subtitle> <message>`);
    }
} catch (error) {
    console.error(error.message);
    process.exit(1);
}
