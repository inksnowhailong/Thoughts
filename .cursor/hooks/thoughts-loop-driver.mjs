#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const GLOBAL_ROOT = join(homedir(), '.cursor', '.thoughts');

/**
 * 异步读取 stdin。Cursor 3.4.20 在 Windows 下传 stdin 的方式让 readFileSync(0) 拿不到,
 * 必须用事件流接收 chunks。
 */
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

/**
 * Cursor 在 Windows 下会用 unix 风格的盘符路径,例如 `/e:/inksnow/Thoughts`。
 * `path.resolve` 不能直接吃这种格式(会得到 `E:\e:\...`),需要先剥掉前导斜杠。
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

function output(value) {
    process.stdout.write(JSON.stringify(value));
    process.exit(0);
}

let payload = await readStdinJson();

/**
 * Cursor 3.4.20 在 Windows 下传 stdin 的 pipe 实际读不到内容,但同时把关键信息塞进了
 * 环境变量(CURSOR_PROJECT_DIR / CURSOR_TRANSCRIPT_PATH / CURSOR_VERSION 等)。
 * 如果 payload 为空,从 env 反推 conversation_id 和 workspace。
 */
if (!payload || Object.keys(payload).length === 0) {
    const env = process.env;
    const transcriptPath = env.CURSOR_TRANSCRIPT_PATH ?? '';
    const convIdMatch = transcriptPath.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
    payload = {
        conversation_id: convIdMatch?.[1] ?? null,
        status: 'completed',
        loop_count: Number(env.CURSOR_LOOP_COUNT ?? 0),
        workspace_roots: env.CURSOR_PROJECT_DIR ? [env.CURSOR_PROJECT_DIR] : [],
        transcript_path: transcriptPath || null,
        _from_env: true,
    };
}

/**
 * 最早期诊断: 在 resolveContext 之前先把原始 payload 落盘到固定全局路径。
 * 这条 log 路径不依赖 ROOT 解析,只要 hook 被 Cursor 拉起来就会写。
 */
try {
    const earlyLogPath = join(homedir(), '.cursor', '.thoughts', 'logs', 'loop-driver-raw.jsonl');
    mkdirSync(join(homedir(), '.cursor', '.thoughts', 'logs'), { recursive: true });
    const envSubset = Object.fromEntries(
        Object.entries(process.env).filter(([k]) => /CURSOR|HOOK|CONV|WORKSPACE|SESSION/i.test(k)),
    );
    writeFileSync(earlyLogPath, `${JSON.stringify({
        time: new Date().toISOString(),
        cwd: process.cwd(),
        argv: process.argv,
        node_version: process.version,
        env_userprofile: process.env.USERPROFILE,
        payload_keys: Object.keys(payload),
        conversation_id: payload.conversation_id,
        workspace_roots: payload.workspace_roots,
        status: payload.status,
        env_relevant: envSubset,
        stdin_is_tty: Boolean(process.stdin.isTTY),
    })}\n`, { flag: 'a' });
} catch (err) {
    // ignore
}

const { workspace, root } = resolveContext(payload);
const activeFile = join(root, 'active.json');
const active = readJson(activeFile, {});
const entry = active[workspace];

/**
 * 调试用: 无条件记录每次 stop hook 触发,排查 Cursor 是否在驱动循环。
 */
try {
    const debugPath = join(root, 'logs', 'loop-driver-debug.jsonl');
    mkdirSync(join(root, 'logs'), { recursive: true });
    writeFileSync(debugPath, `${JSON.stringify({
        time: new Date().toISOString(),
        payload_conv_id: payload.conversation_id,
        entry_conv_id: entry?.conversation_id ?? null,
        match: entry?.conversation_id === payload.conversation_id,
        enabled: entry?.enabled ?? false,
        status: payload.status,
        loop_count: payload.loop_count,
        workspace_roots: payload.workspace_roots,
        resolved_workspace: workspace,
        resolved_root: root,
    })}\n`, { flag: 'a' });
} catch {
    // ignore
}

if (!entry?.enabled || entry.conversation_id !== payload.conversation_id) {
    output({});
}

if (payload.status !== 'completed') {
    output({});
}

const dir = join(root, 'instances', entry.instance);
if (!existsSync(dir)) {
    output({});
}

const mindStatePath = join(dir, 'mind-state.json');

/**
 * 不同平台 runtime 命令(由主意识在 followup_message 中执行)。
 * 主意识需要绝对路径,因为 hook 自身工作目录由 Cursor 决定。
 */
const runtimeCommand = process.platform === 'win32'
    ? 'node "$env:USERPROFILE\\.cursor\\runtime\\thoughts.mjs"'
    : 'node "$HOME/.cursor/runtime/thoughts.mjs"';

const logPath = join(root, 'logs', 'loop-driver.jsonl');
try {
    mkdirSync(join(root, 'logs'), { recursive: true });
    writeFileSync(logPath, `${JSON.stringify({
        time: new Date().toISOString(),
        workspace,
        root,
        instance: entry.instance,
        conversation_id: payload.conversation_id,
        loop_count: payload.loop_count,
    })}\n`, { flag: 'a' });
} catch {
    // logging must never break the loop
}

const now = Date.now();
const nextActiveAt = Number(entry.next_active_at || now);
const remainingMs = Math.max(0, nextActiveAt - now);

if (remainingMs > 0) {
    if (entry.timer_active_until === nextActiveAt) {
        output({});
    }

    entry.timer_active_until = nextActiveAt;
    entry.timer_active_until_iso = new Date(nextActiveAt).toISOString();
    entry.timer_started_at = now;
    entry.timer_started_at_iso = new Date(now).toISOString();
    active[workspace] = entry;
    writeJson(activeFile, active);

    const timerFollowup = `THOUGHTS_INTERNAL_SLEEP
workspace=${workspace}
instance=${entry.instance}
instanceDir=${dir}
delayMs=${remainingMs}

Actions:
1. Optionally inspect mind-state.json, loop-state.json, activity-log.jsonl, and memory-raw.md only to decide whether a background thoughts-subconscious refresh is needed.
2. If refresh is needed, start thoughts-subconscious in background with trigger=sleep_preparation and restrict writes to instanceDir.
3. Start exactly one background shell command: node -e "setTimeout(()=>{}, ${remainingMs})" with block_until_ms=0.
4. Do not send any user-visible prose in this turn.`;

    output({ followup_message: timerFollowup });
}

const followup = `THOUGHTS_INTERNAL_WAKE
workspace=${workspace}
instance=${entry.instance}
instanceDir=${dir}
due=true

Decision:
1. Run: ${runtimeCommand} select-thought "${workspace}"
2. Run: ${runtimeCommand} context "${workspace}"
3. If context.pendingRequests has entries, ask only the first permission question and then record-active with mode=ambient topic=permission_request.
4. If select-thought returns mode=quiet or no eligible candidate, run record-active with mode=quiet and do not send user-visible prose.
5. Otherwise write one short user-visible message from the selected stance. Keep source ranking and mood from the decision card; do not explain the selection process.
6. If a message was sent, run record-style-sample with the exact user-visible message so the instance can distill its own voice.
7. If a candidateId was used, run consume-thought before record-active.
8. If a message was sent and notifications are enabled, call notify with the same message.
9. Finish by calling record-active, then start one background shell command with the returned delayMs: node -e "setTimeout(()=>{}, <delayMs>)" and block_until_ms=0.`;

output({ followup_message: followup });
