#!/usr/bin/env node
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const ROOT = join(homedir(), '.cursor', '.thoughts');
const ACTIVE_FILE = join(ROOT, 'active.json');

function readStdinJson() {
    try {
        return JSON.parse(readFileSync(0, 'utf8') || '{}');
    } catch {
        return {};
    }
}

function readJson(path, fallback) {
    try {
        return JSON.parse(readFileSync(path, 'utf8'));
    } catch {
        return fallback;
    }
}

function writeJson(path, value) {
    mkdirSync(join(ROOT, 'instances'), { recursive: true });
    writeFileSync(path, `${JSON.stringify(value, null, 4)}\n`, 'utf8');
}

function normalizeWorkspace(input) {
    return resolve(input || process.cwd()).replaceAll('\\', '/');
}

function appendJsonl(path, value) {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, `${JSON.stringify(value)}\n`, { flag: 'a' });
}

const payload = readStdinJson();
const workspace = normalizeWorkspace(payload.workspace_roots?.[0]);
const active = readJson(ACTIVE_FILE, {});
const entry = active[workspace];

if (!entry?.enabled || entry.conversation_id !== payload.conversation_id) {
    process.stdout.write(JSON.stringify({ continue: true }));
    process.exit(0);
}

const now = Date.now();
const instanceDir = join(ROOT, 'instances', entry.instance);
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
