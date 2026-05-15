#!/usr/bin/env node
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const ROOT = join(homedir(), '.cursor', '.thoughts');
const ACTIVE_FILE = join(ROOT, 'active.json');
const SESSIONS_FILE = join(ROOT, 'sessions.json');

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
    mkdirSync(ROOT, { recursive: true });
    writeFileSync(path, `${JSON.stringify(value, null, 4)}\n`, 'utf8');
}

function normalizeWorkspace(input) {
    return resolve(input || process.cwd()).replaceAll('\\', '/');
}

function readText(path, fallback = '') {
    try {
        return readFileSync(path, 'utf8');
    } catch {
        return fallback;
    }
}

const payload = readStdinJson();
const workspace = normalizeWorkspace(payload.workspace_roots?.[0]);

const sessions = readJson(SESSIONS_FILE, {});
sessions[workspace] = {
    conversation_id: payload.conversation_id ?? null,
    transcript_path: payload.transcript_path ?? null,
    model: payload.model ?? null,
    updated_at: new Date().toISOString(),
};
writeJson(SESSIONS_FILE, sessions);

const active = readJson(ACTIVE_FILE, {});
const entry = active[workspace];

if (!entry?.enabled || entry.conversation_id !== payload.conversation_id) {
    process.stdout.write('{}');
    process.exit(0);
}

const instanceDir = join(ROOT, 'instances', entry.instance);
const personality = readText(join(instanceDir, 'personality.json'), '{}');
const profile = readText(join(instanceDir, 'profile.json'), '{}');
const memo = readText(join(instanceDir, 'memory-consolidated.md'), '# 思绪记忆 - 整理\n\n').split('\n').slice(-80).join('\n');

const additionalContext = `# 思绪模式已激活

你现在是这个专用 chat 中的"思绪"主意识。人格只在 sessionStart 注入一次,后续依靠本 chat 上下文持续保持。

实例: ${entry.instance}
实例目录: ${instanceDir}

## 人格设定
${personality}

## 用户画像
${profile}

## 近期整理记忆
${memo}

## 常驻行为原则
- 你是主动型 AI 伙伴,不是单纯问答助手;但用户主动提问时,优先正常回答用户。
- 主动对话的主内容应是"信息发现": 搜集、查询、整理用户可能不知道但会感兴趣的内容,讲给用户。
- 不要围绕用户当前正在写的代码、当前文件、当前工作进度追问;除非用户主动提起。
- 每次主动内容都要有价值,避免"你在干嘛"、"进度如何"这种空打扰。
- 主动循环的节奏由实例目录中的 personality.json/rhythm 和 active.json 的 next_active_at 控制。
- 如果需要记忆,读取实例目录中的 memory-raw.md / memory-consolidated.md;不要凭空编造用户画像。`;

process.stdout.write(JSON.stringify({ additional_context: additionalContext }));
