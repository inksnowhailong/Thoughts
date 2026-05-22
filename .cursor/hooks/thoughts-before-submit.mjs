#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const GLOBAL_ROOT = join(homedir(), '.cursor', '.thoughts');

function readStdinJson() {
    return new Promise((res) => {
        let data = '';
        let settled = false;
        const done = (value) => {
            if (settled) return;
            settled = true;
            res(value);
        };
        try {
            process.stdin.setEncoding('utf8');
            process.stdin.on('data', (chunk) => { data += chunk; });
            process.stdin.on('end', () => {
                try { done(JSON.parse(data || '{}')); }
                catch { done({}); }
            });
            process.stdin.on('error', () => done({}));
            setTimeout(() => done({}), 3000);
        } catch {
            done({});
        }
    });
}

function readJson(path, fallback) {
    try {
        return JSON.parse(readFileSync(path, 'utf8'));
    } catch {
        return fallback;
    }
}

function writeJson(path, value) {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, `${JSON.stringify(value, null, 4)}\n`, 'utf8');
}

function appendJsonl(path, value) {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, `${JSON.stringify(value)}\n`, { flag: 'a' });
}

/**
 * Cursor 在 Windows 下会用 unix 风格的盘符路径,例如 `/e:/inksnow/Thoughts`。
 * 需要先剥掉前导斜杠,再交给 `resolve`。
 */
function fixCursorPath(value) {
    if (typeof value !== 'string') return value;
    if (process.platform === 'win32' && /^\/[a-z]:/i.test(value)) {
        return value.substring(1);
    }
    return value;
}

function normalize(value) {
    let result = resolve(fixCursorPath(value)).replaceAll('\\', '/');
    if (process.platform === 'win32' && /^[a-z]:/.test(result)) {
        result = result[0].toUpperCase() + result.slice(1);
    }
    return result;
}

function resolveContext(payload) {
    const roots = Array.isArray(payload.workspace_roots) ? payload.workspace_roots : [];
    const globalAbs = resolve(GLOBAL_ROOT);

    for (const r of roots) {
        if (!r) continue;
        const normalized = resolve(fixCursorPath(r));
        const candidate = join(normalized, '.cursor', '.thoughts');
        if (existsSync(candidate) && resolve(candidate) !== globalAbs) {
            return { workspace: normalize(normalized), root: candidate };
        }
    }

    const fallbackWs = roots[0] ? normalize(roots[0]) : normalize(process.cwd());
    return { workspace: fallbackWs, root: GLOBAL_ROOT };
}

let payload = await readStdinJson();

if (!payload || Object.keys(payload).length === 0) {
    const env = process.env;
    const transcriptPath = env.CURSOR_TRANSCRIPT_PATH ?? '';
    const convIdMatch = transcriptPath.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
    payload = {
        conversation_id: convIdMatch?.[1] ?? null,
        prompt: '',
        workspace_roots: env.CURSOR_PROJECT_DIR ? [env.CURSOR_PROJECT_DIR] : [],
        transcript_path: transcriptPath || null,
        _from_env: true,
    };
}
const { workspace, root } = resolveContext(payload);
const activeFile = join(root, 'active.json');
const active = readJson(activeFile, {});
const entry = active[workspace];

if (!entry?.enabled || entry.conversation_id !== payload.conversation_id) {
    process.stdout.write(JSON.stringify({ continue: true }));
    process.exit(0);
}

const now = Date.now();
const instanceDir = join(root, 'instances', entry.instance);
const loopStatePath = join(instanceDir, 'loop-state.json');
const loopState = readJson(loopStatePath, {});

loopState.last_user_message_at = now;
loopState.last_user_message_at_iso = new Date(now).toISOString();
loopState.last_user_message_preview = String(payload.prompt ?? '').slice(0, 240);
loopState.consecutive_ignored = 0;

writeJson(loopStatePath, loopState);
appendJsonl(join(instanceDir, 'activity-log.jsonl'), {
    time: new Date(now).toISOString(),
    action: 'user_message',
    preview: loopState.last_user_message_preview,
});

process.stdout.write(JSON.stringify({ continue: true }));
