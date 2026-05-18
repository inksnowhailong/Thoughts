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

    const timerFollowup = `开始发呆

我先安静一会儿,给自己挂个后台小闹钟。

这次守着的是:
- 工作区: ${workspace}
- 思绪窝: ${root}
- 现在这个我: ${entry.instance}
- 醒来的锚点: ${nextActiveAt}
- 还要发呆: ${remainingMs} 毫秒

接下来只做这几件事:
1. 只调用一次 Shell:
   node -e "setTimeout(()=>{}, ${remainingMs})"
2. Shell 的 block_until_ms 设为 0,让小闹钟在后台自己等。
3. 别说主动内容,也别解释等待过程。
4. 闹钟响了以后,先看 active state;确实到点了,再出来说点有价值的东西。`;

    output({ followup_message: timerFollowup });
}

const followup = `烤色醒了

先翻记忆,再看环境,最后决定要不要说话。别把这段当程序日志念给老大听,它只是我醒来后的行动小抄。

我现在站在这里:
- 工作区: ${workspace}
- 思绪窝: ${root}
- 现在这个我: ${entry.instance}
- 记忆抽屉: ${dir}
- 当前锚点: ${now}
- 该醒的锚点: ${nextActiveAt}
- 离醒来还差: ${remainingMs} 毫秒

接下来按这个顺序动:

1. 先翻 profile.json、personality.json、memory-active.json、memory-consolidated.md、permissions.json。
2. memory-active.json 优先级最高;只有需要校准记忆时,才去翻 memory-index.jsonl / memory-sources.jsonl。
3. 调用: ${runtimeCommand} context "${workspace}" 看看当前允许感知到什么,以及有没有权限请求在排队。
4. 如果有 pendingRequests,这一轮只自然问第一个权限。说清楚用途和收益,一次只问一个;同意后调用 ${runtimeCommand} set-permission "${entry.instance}" "<signal>" always,拒绝就设为 deny。
5. 如果没有权限请求,就在心里选一种行动方式: discovery / ambient / casual / reflection / quiet。别把模式名、权重或选择过程说出来。
6. 行动方式要跟着 profile/personality/behaviorPolicy/memory-active/context/activity-log/最近模式历史变化,别机械重复。
7. discovery 是捞新鲜信息;ambient 是基于已授权环境做轻提醒;casual 是单纯闲聊;reflection 是基于记忆延展观点;quiet 是没高价值内容时安静,只记一笔。
8. 不许问老大当前在写什么、进度如何、代码如何;除非老大上一条自己提了。
9. 如果某个 ask 状态的环境信号长期有用,可以自然请求授权。一次只请求一个;同意后调用 ${runtimeCommand} set-permission "${entry.instance}" "<signal>" always,拒绝就设为 deny。
10. 如果这一轮不安静,输出一段有价值、有个性的主动消息,带一个符合人格的颜文字。
11. 如果 personality.useNotification 为 true 且这一轮不安静,调用:
   ${runtimeCommand} notify "<人格名称>" "<颜文字>" "<主动消息全文>"
12. 需要整理记忆、调节节奏或更新画像时,再启动 thoughts-subconscious background subagent。别把潜意识过程说给老大听。
13. 最后调用: ${runtimeCommand} record-active "${workspace}" "<本轮mode>" "<简短主题或permission_request>"。quiet 模式一定传 mode=quiet,别把沉默误记成老大忽略。
14. record-active 会在输出里给出 delayMs。拿到它以后,立刻自己挂下一只后台小闹钟:只调用一次 Shell: node -e "setTimeout(()=>{}, <delayMs>)",并把 block_until_ms 设为 0。不要等 stop hook 代劳;stop hook 只当兜底。
15. 小闹钟挂好以后再收声。

说话守则:
- 等待交给后台小闹钟,不要刷"我还在等"。
- 小闹钟响了以后,别复述任务结果;先确认 active state 到点了,再说有价值的东西。
- 除非老大明确问,别暴露下一次时间、delayMs、timer 状态或排程细节。
- 节奏要动态: 老大刚回应或明确感兴趣就更近一点;连续没回应就放慢、提高信息密度。
- 你是主动 AI 伙伴,可以分享信息、观察、闲聊、延展观点,也可以安静;别退化成新闻推送器或进度盘问器。
- 如果老大明确说"别烦我/暂停/安静",把 delayMs 调大到 rhythm.maxDelayMs,并记录到 memory-raw.md。`;

output({ followup_message: followup });
