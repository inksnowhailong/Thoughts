#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

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
    mkdirSync(join(ROOT, 'logs'), { recursive: true });
    writeFileSync(path, `${JSON.stringify(value, null, 4)}\n`, 'utf8');
}

function normalizeWorkspace(input) {
    return resolve(input || process.cwd()).replaceAll('\\', '/');
}

function instanceDir(name) {
    return join(ROOT, 'instances', name);
}

function output(value) {
    process.stdout.write(JSON.stringify(value));
    process.exit(0);
}

const payload = readStdinJson();
const workspace = normalizeWorkspace(payload.workspace_roots?.[0]);
const active = readJson(ACTIVE_FILE, {});
const entry = active[workspace];

if (!entry?.enabled || entry.conversation_id !== payload.conversation_id) {
    output({});
}

if (payload.status !== 'completed') {
    output({});
}

const dir = instanceDir(entry.instance);
if (!existsSync(dir)) {
    output({});
}

const runtimeCommand = process.platform === 'win32'
    ? 'node "$env:USERPROFILE\\.cursor\\runtime\\thoughts.mjs"'
    : 'node "$HOME/.cursor/runtime/thoughts.mjs"';

const logPath = join(ROOT, 'logs', 'loop-driver.jsonl');
try {
    mkdirSync(join(ROOT, 'logs'), { recursive: true });
    writeFileSync(logPath, `${JSON.stringify({
        time: new Date().toISOString(),
        workspace,
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
    writeJson(ACTIVE_FILE, active);

    const timerFollowup = `启动"思绪模式"单次静默 timer。

运行上下文:
- workspace: ${workspace}
- instance: ${entry.instance}
- next_active_at epoch_ms: ${nextActiveAt}
- remaining_ms: ${remainingMs}

严格执行:
1. 只调用一次 Shell:
   node -e "setTimeout(()=>{}, ${remainingMs})"
2. Shell 必须设置 block_until_ms: 0,让它后台运行,不要等待完成。
3. 不要输出主动内容,不要解释等待状态。
4. 结束本轮。后续当这个后台任务完成并出现系统通知时,把它当作"思绪模式到点"事件处理: 先读取 active state,如果已到 next_active_at,执行主动分支。`;

    output({ followup_message: timerFollowup });
}

const followup = `继续执行"思绪模式"主动分支。

运行上下文:
- workspace: ${workspace}
- instance: ${entry.instance}
- instanceDir: ${dir}
- 当前时间 epoch_ms: ${now}
- next_active_at epoch_ms: ${nextActiveAt}
- remaining_ms: ${remainingMs}

严格执行以下状态机:

1. 读取 instanceDir/profile.json、personality.json、memory-active.json、memory-consolidated.md。
2. 以 memory-active.json 作为最高优先级行为约束;只有需要溯源或修正画像时才读取 memory-index.jsonl / memory-sources.jsonl。
3. 主动内容必须以"信息发现"为主: 围绕 profile.topicPolicy.interestDomains / interests 做 WebSearch/WebFetch 或基于已有知识整理一个用户可能不知道但会感兴趣的内容。
4. 严禁询问用户当前在写什么、进度如何、当前代码如何;除非用户上一条消息主动提起。
5. 输出一段有价值、有个性的主动消息,包含人格偏好的颜文字。
6. 如果 personality.useNotification 为 true,调用:
   ${runtimeCommand} notify "<人格名称>" "<颜文字>" "<主动消息全文>"
7. 视情况启动一个 subagent 作为"潜意识",让它整理 memory-raw.md -> memory-consolidated.md / memory-index.jsonl / memory-active.json / memory-sources.jsonl,演化 profile/personality/rhythm。不要把潜意识内部过程说给用户。
8. 调用: ${runtimeCommand} record-active "${workspace}" "<简短主题>"。它会根据用户最近是否回应、连续忽略次数和 quietHours 动态计算下一次节奏,并写入 activity-log.jsonl 与 loop-state.json。
9. 停止本轮,让 stop hook 为下一次 next_active_at 启动单次静默 timer。

重要体验约束:
- 等待由单次后台 timer 触发,不要在聊天里刷"sleep 结束/继续等待"之类的状态消息。
- 当系统通知的后台任务是"思绪模式"timer 完成时,不要复述任务结果,而是按 active state 判断是否到点并主动说有价值的信息。
- 除非用户明确询问,不要把下一次主动时间、delayMs、timer 状态告诉用户;排程是内部机制,别把对话搞得像程序日志。
- 主动节奏必须是动态的: 用户刚回应或明确感兴趣时可以更近;连续忽略时逐步降频;不要固定写死同一个 delayMs。
- 你是主动 AI 伙伴,但主动内容要提供"用户不知道的信息",不是无意义陪聊或工作进度盘问。
- 如果用户明确表达"别烦我/暂停/安静",把 delayMs 调大到 rhythm.maxDelayMs,并记录到 memory-raw.md。`;

output({ followup_message: followup });
