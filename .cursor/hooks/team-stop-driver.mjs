#!/usr/bin/env node
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';

// 调试日志：仅在 THOUGHTS_TEAM_DEBUG=1 时启用，避免污染正常运行。
// 日志写到 <workspace>/.cursor/.team/logs/stop.log，每行一个事件，方便排查静默失败。
const DEBUG_ENABLED = Boolean(process.env.THOUGHTS_TEAM_DEBUG);
let debugLogPath = null;

function debugSetup(workspaceRoot) {
    if (!DEBUG_ENABLED || !workspaceRoot) return;
    debugLogPath = join(workspaceRoot, '.cursor', '.team', 'logs', 'stop.log');
    try {
        mkdirSync(dirname(debugLogPath), { recursive: true });
    } catch {
        debugLogPath = null;
    }
}

function debug(event, payload) {
    if (!DEBUG_ENABLED || !debugLogPath) return;
    try {
        const line = JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, event, ...payload });
        appendFileSync(debugLogPath, `${line}\n`);
    } catch {
        // 日志失败不能影响主流程。
    }
}

function readStdinJson() {
    return new Promise((resolveValue) => {
        let data = '';
        let settled = false;
        const done = (value) => {
            if (settled) return;
            settled = true;
            resolveValue(value);
        };
        try {
            process.stdin.setEncoding('utf8');
            process.stdin.on('data', (chunk) => { data += chunk; });
            process.stdin.on('end', () => {
                try { done(JSON.parse(data || '{}')); } catch { done({}); }
            });
            process.stdin.on('error', () => done({}));
            setTimeout(() => done({}), 3000);
        } catch {
            done({});
        }
    });
}

function fixCursorPath(value) {
    if (typeof value !== 'string') return value;
    if (process.platform === 'win32' && /^\/[a-z]:/i.test(value)) return value.slice(1);
    return value;
}

function normalize(value) {
    let result = resolve(fixCursorPath(value)).replaceAll('\\', '/');
    if (process.platform === 'win32' && /^[a-z]:/.test(result)) {
        result = result[0].toUpperCase() + result.slice(1);
    }
    return result;
}

function readJson(path, fallback) {
    try {
        const text = readFileSync(path, 'utf8');
        return JSON.parse(text.charCodeAt(0) === 0xfeff ? text.slice(1) : text);
    } catch {
        return fallback;
    }
}

function output(value) {
    process.stdout.write(JSON.stringify(value));
    process.exit(0);
}

function resolveContext(payload) {
    const roots = Array.isArray(payload.workspace_roots) ? payload.workspace_roots : [];
    const workspace = roots[0]
        ? normalize(roots[0])
        : process.env.CURSOR_PROJECT_DIR
            ? normalize(process.env.CURSOR_PROJECT_DIR)
            : normalize(process.cwd());

    let conversationId = payload.conversation_id ?? payload.conversationId ?? null;
    if (!conversationId && process.env.CURSOR_TRANSCRIPT_PATH) {
        const match = process.env.CURSOR_TRANSCRIPT_PATH.match(/[\\/]([0-9a-f-]{36})[\\/.]/i);
        if (match) conversationId = match[1];
    }

    return { workspace, conversationId };
}

function pendingCount(teamDir, agent) {
    const inbox = join(teamDir, 'inbox', agent);
    if (!existsSync(inbox)) return 0;
    let total = 0;
    for (const item of readdirSync(inbox, { withFileTypes: true })) {
        if (!item.isDirectory() || !item.name.startsWith('from-')) continue;
        const dir = join(inbox, item.name);
        total += readdirSync(dir).filter((file) => file.endsWith('.json') && !file.includes('.tmp')).length;
    }
    return total;
}

function processMessage(agent, count) {
    return [
        `[team] ${agent} 有 ${count} 条未处理消息。`,
        '',
        '请按当前 skill 的主循环处理：',
        `1. 调用 \`node .cursor/runtime/team.mjs mailbox-consume . ${agent}\` 一次性取出并删除消息。`,
        '2. 如果你是 leader：处理回执、推进 Critic / Review 链、按需继续派任务。',
        '3. 如果你是 sub：完成任务后只给 leader 写回执，不要直接转发给其他 sub。',
        '4. 处理完后停止本轮，让 stop hook 重新进入等待。',
        '',
        '不要把邮箱、watcher、hook 这些内部词复述给用户，除非用户正在调试机制。',
    ].join('\n');
}

function waitMessage(agent) {
    return [
        `[team] ${agent} 当前没有待处理消息。`,
        '',
        '请启动后台等待：',
        `node .cursor/runtime/team.mjs mailbox-watch . ${agent} --max-ms 1800000`,
        '',
        'Shell 调用时使用 block_until_ms: 0。启动后停止本轮，不要输出用户可见状态消息。',
        '如果 chokidar 已全局安装，runtime 会优先使用；否则会回退到 fs.watch + 轮询。',
    ].join('\n');
}

const payload = await readStdinJson();
const { workspace, conversationId } = resolveContext(payload);

const workspaceForLog = workspace ? fixCursorPath(workspace) : null;
debugSetup(workspaceForLog);
debug('enter', { workspace, conversationId, hasStdinPayload: Object.keys(payload).length > 0 });

if (!conversationId) {
    debug('exit-no-conversation-id', {});
    output({});
}

const teamDir = join(fixCursorPath(workspace), '.cursor', '.team');
const config = readJson(join(teamDir, 'config.json'), null);
if (!config?.bindings) {
    debug('exit-no-config-or-bindings', { configExists: Boolean(config), hasBindings: Boolean(config?.bindings) });
    output({});
}

const agent = config.bindings[conversationId];
if (!agent) {
    debug('exit-no-binding-for-conv', { conversationId, knownConversationIds: Object.keys(config.bindings) });
    output({});
}

const count = pendingCount(teamDir, agent);
debug('decision', { agent, pendingCount: count, branch: count > 0 ? 'process' : 'wait' });
output({ followup_message: count > 0 ? processMessage(agent, count) : waitMessage(agent) });
