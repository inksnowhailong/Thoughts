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

function appendJsonl(path, value) {
    ensureDir(dirname(path));
    writeFileSync(path, `${JSON.stringify(value)}\n`, { flag: 'a' });
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
    delete entry.timer_active_until;
    delete entry.timer_active_until_iso;
    delete entry.timer_started_at;
    delete entry.timer_started_at_iso;
    entry.updated_at = new Date().toISOString();
    state[workspace] = entry;
    saveActive(state);
    console.log(JSON.stringify(entry, null, 4));
}

function loopStatePath(instanceName) {
    return join(instanceDir(instanceName), 'loop-state.json');
}

function activityLogPath(instanceName) {
    return join(instanceDir(instanceName), 'activity-log.jsonl');
}

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

function isQuietHour(now, quietHours = []) {
    if (!Array.isArray(quietHours) || quietHours.length !== 2) return false;

    const [start, end] = quietHours.map(Number);
    const hour = new Date(now).getHours();

    if (start === end) return false;
    if (start < end) return hour >= start && hour < end;
    return hour >= start || hour < end;
}

function recordUser(workspaceArg, preview = '') {
    const workspace = normalizeWorkspace(workspaceArg);
    const state = activeState();
    const entry = state[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const now = Date.now();
    const path = loopStatePath(entry.instance);
    const loopState = readJson(path, {});

    loopState.last_user_message_at = now;
    loopState.last_user_message_at_iso = new Date(now).toISOString();
    loopState.last_user_message_preview = String(preview ?? '').slice(0, 240);
    loopState.consecutive_ignored = 0;

    writeJson(path, loopState);
    appendJsonl(activityLogPath(entry.instance), {
        time: new Date(now).toISOString(),
        action: 'user_message',
        preview: loopState.last_user_message_preview,
    });

    console.log(JSON.stringify(loopState, null, 4));
}

function recordActive(workspaceArg, topic = 'active_message') {
    const workspace = normalizeWorkspace(workspaceArg);
    const state = activeState();
    const entry = state[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const now = Date.now();
    const dir = instanceDir(entry.instance);
    const personality = readJson(join(dir, 'personality.json'), {});
    const rhythm = personality.rhythm ?? {};
    const minDelayMs = Number(rhythm.minDelayMs ?? 15 * 60 * 1000);
    const baseDelayMs = Number(rhythm.baseDelayMs ?? 30 * 60 * 1000);
    const maxDelayMs = Number(rhythm.maxDelayMs ?? 2 * 60 * 60 * 1000);
    const decayMultiplier = Number(rhythm.decayMultiplier ?? 1.5);
    const boostMultiplier = Number(rhythm.boostMultiplier ?? 0.7);

    const path = loopStatePath(entry.instance);
    const loopState = readJson(path, {});
    const previousActiveAt = Number(loopState.last_active_message_at || 0);
    const lastUserAt = Number(loopState.last_user_message_at || 0);
    const previousActiveWasIgnored = previousActiveAt > 0 && lastUserAt < previousActiveAt;
    const consecutiveIgnored = previousActiveWasIgnored
        ? Number(loopState.consecutive_ignored || 0) + 1
        : 0;

    let delayMs;
    let delayReason;

    if (isQuietHour(now, rhythm.quietHours)) {
        delayMs = maxDelayMs;
        delayReason = 'quiet hours';
    } else if (consecutiveIgnored >= 3) {
        delayMs = maxDelayMs;
        delayReason = 'three or more consecutive ignored messages';
    } else if (consecutiveIgnored > 0) {
        delayMs = clamp(Math.round(baseDelayMs * (decayMultiplier ** consecutiveIgnored)), minDelayMs, maxDelayMs);
        delayReason = `${consecutiveIgnored} consecutive ignored message(s)`;
    } else if (lastUserAt > previousActiveAt && now - lastUserAt <= 20 * 60 * 1000) {
        delayMs = clamp(Math.round(baseDelayMs * boostMultiplier), minDelayMs, maxDelayMs);
        delayReason = 'recent user engagement';
    } else {
        delayMs = clamp(baseDelayMs, minDelayMs, maxDelayMs);
        delayReason = 'baseline rhythm';
    }

    loopState.consecutive_ignored = consecutiveIgnored;
    loopState.last_active_message_at = now;
    loopState.last_active_message_at_iso = new Date(now).toISOString();
    loopState.last_active_topic = topic;
    loopState.last_delay_ms = delayMs;
    loopState.last_delay_reason = delayReason;
    writeJson(path, loopState);

    appendJsonl(activityLogPath(entry.instance), {
        time: new Date(now).toISOString(),
        action: 'active_message',
        topic,
        delayMs,
        delayReason,
        consecutiveIgnored,
    });

    const nextActiveAt = now + delayMs;
    entry.next_active_at = nextActiveAt;
    entry.next_active_at_iso = new Date(nextActiveAt).toISOString();
    entry.last_schedule_reason = `dynamic: ${delayReason}`;
    delete entry.timer_active_until;
    delete entry.timer_active_until_iso;
    delete entry.timer_started_at;
    delete entry.timer_started_at_iso;
    entry.updated_at = new Date(now).toISOString();
    state[workspace] = entry;
    saveActive(state);

    console.log(JSON.stringify({
        delayMs,
        delayReason,
        consecutiveIgnored,
        next_active_at: nextActiveAt,
        next_active_at_iso: entry.next_active_at_iso,
    }, null, 4));
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
        'memory-active.json': '{\n    "version": 1,\n    "updatedAt": null,\n    "items": []\n}\n',
        'memory-index.jsonl': '',
        'memory-sources.jsonl': '',
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
        case 'record-user':
            recordUser(args[0], args.slice(1).join(' '));
            break;
        case 'record-active':
            recordActive(args[0], args.slice(1).join(' '));
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
  node .cursor/runtime/thoughts.mjs record-user [workspace] [preview]
  node .cursor/runtime/thoughts.mjs record-active [workspace] [topic]
  node .cursor/runtime/thoughts.mjs state [workspace]
  node .cursor/runtime/thoughts.mjs notify <title> <subtitle> <message>`);
    }
} catch (error) {
    console.error(error.message);
    process.exit(1);
}
