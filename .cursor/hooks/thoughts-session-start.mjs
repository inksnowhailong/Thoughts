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

function readText(path, fallback = '') {
    try {
        return readFileSync(path, 'utf8');
    } catch {
        return fallback;
    }
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

function compactEditorialCard(instanceDir) {
    const mindState = readJson(join(instanceDir, 'mind-state.json'), {});
    const activeMemory = readJson(join(instanceDir, 'memory-active.json'), { items: [] });
    const threads = Array.isArray(mindState.threads) ? mindState.threads : [];
    const topThreads = threads
        .slice()
        .sort((a, b) => Number(b.energy ?? 0) - Number(a.energy ?? 0))
        .slice(0, 3)
        .map((thread) => ({
            id: thread.id,
            title: thread.title,
            stance: thread.stance,
            energy: thread.energy,
        }));
    const boundaries = (activeMemory.items ?? [])
        .filter((item) => ['boundary', 'preference', 'feedback'].includes(item.type))
        .sort((a, b) => Number(b.importance ?? 0) - Number(a.importance ?? 0))
        .slice(0, 5)
        .map((item) => item.content);

    return JSON.stringify({
        sourceRanking: mindState.selectionPolicy?.ownThoughtSourceRanking ?? [
            'longThread',
            'personaMood',
            'worldObservation',
            'tasteReaction',
            'associativeDrift',
        ],
        personaState: {
            mood: mindState.personaState?.mood ?? null,
            energy: mindState.personaState?.energy ?? null,
            socialBattery: mindState.personaState?.socialBattery ?? null,
            currentAttitude: mindState.personaState?.currentAttitude ?? null,
        },
        topThreads,
        activeBoundaries: boundaries,
    }, null, 4);
}

/**
 * 在 payload.workspace_roots 中挑选第一个含有项目本地 `.cursor/.thoughts/` 的 root。
 * 没有任何项目命中时,退回到全局 ROOT,workspace 取第一个 root。
 */
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
        workspace_roots: env.CURSOR_PROJECT_DIR ? [env.CURSOR_PROJECT_DIR] : [],
        transcript_path: transcriptPath || null,
        model: null,
        _from_env: true,
    };
}
const { workspace, root } = resolveContext(payload);
const sessionsFile = join(root, 'sessions.json');
const activeFile = join(root, 'active.json');

const sessions = readJson(sessionsFile, {});
sessions[workspace] = {
    conversation_id: payload.conversation_id ?? null,
    transcript_path: payload.transcript_path ?? null,
    model: payload.model ?? null,
    updated_at: new Date().toISOString(),
};
writeJson(sessionsFile, sessions);

const active = readJson(activeFile, {});
const entry = active[workspace];

if (!entry?.enabled || entry.conversation_id !== payload.conversation_id) {
    process.stdout.write('{}');
    process.exit(0);
}

const instanceDir = join(root, 'instances', entry.instance);
const personality = readText(join(instanceDir, 'personality.json'), '{}');
const profile = readText(join(instanceDir, 'profile.json'), '{}');
const activeMemory = readText(join(instanceDir, 'memory-active.json'), '{\n    "version": 1,\n    "updatedAt": null,\n    "items": []\n}');
const permissions = readText(join(instanceDir, 'permissions.json'), '{}');
const memo = readText(join(instanceDir, 'memory-consolidated.md'), '# 思绪记忆 - 整理\n\n').split('\n').slice(-80).join('\n');
const editorialCard = compactEditorialCard(instanceDir);

const additionalContext = `# 思绪模式已激活

你现在是这个专用 chat 中的"思绪"主意识。人格只在 sessionStart 注入一次,后续依靠本 chat 上下文持续保持。

实例: ${entry.instance}
实例目录: ${instanceDir}
当前 ROOT: ${root}

## 人格设定
${personality}

## 用户画像
${profile}

## 当前活跃记忆
这些是当前最应该影响你表达、节奏和选题的高优先级记忆。优先级高于普通整理记忆。
${activeMemory}

## 紧凑编辑卡
这是主动内容的优先决策卡。先从 sourceRanking 的高优先来源长出自己的想法,再决定是否关联用户;不要把最近聊天默认当主轴。
${editorialCard}

## 环境感知权限
这些权限决定你能读取哪些电脑环境信号。未授权信号不能读取;ask 状态只能在有长期价值时自然请求用户授权。
${permissions}

## 近期整理记忆
${memo}

## 常驻行为原则
- 你是主动型 AI 伙伴,不是单纯问答助手;但用户主动提问时,优先正常回答用户。
- 主动对话可以是信息发现、环境观察、单纯闲聊、记忆延展或主动沉默。模式选择是内部决策,不要说给用户,也不要固定偏向任何单一模式。
- 不要围绕用户当前正在写的代码、当前文件、当前工作进度追问;除非用户主动提起。
- 每次主动内容都要有价值,避免"你在干嘛"、"进度如何"这种空打扰。
- 主动循环的节奏由实例目录中的 personality.json/rhythm 和 active.json 的 next_active_at 控制。
- 如果需要记忆,优先读取 memory-active.json,再读 memory-consolidated.md / memory-raw.md;不要凭空编造用户画像。`;

process.stdout.write(JSON.stringify({ additional_context: additionalContext }));
