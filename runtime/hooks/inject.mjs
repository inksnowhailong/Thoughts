#!/usr/bin/env node
// 思绪 — 方案C 在线接力 hook（UserPromptSubmit，跨平台 Node 版）
// 作用：当你在「绑定了某实例的项目」里发消息时，向本轮对话上下文注入：
//   1) 该实例的人格设定 → 让本 chat 以思绪人格回应；
//   2) daemon 后台攒下的、尚未在 chat 里露过面的主动消息 → 让它的话"接力"进对话。
// 读不到绑定实例就静默退出，对其它项目零影响。

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const ROOT = join(homedir(), '.thoughts');

/** 归一化路径，消除 Windows 反斜杠与盘符大小写差异 */
function norm(p) {
    return String(p || '').replaceAll('\\', '/').replace(/\/+$/, '').toLowerCase();
}

function readJson(path, fallback) {
    try {
        return JSON.parse(readFileSync(path, 'utf8'));
    } catch {
        return fallback;
    }
}

/** 读 stdin 的 hook 输入（含 cwd），失败则返回空对象 */
async function readStdin() {
    return new Promise((res) => {
        let data = '';
        let done = false;
        const finish = () => { if (!done) { done = true; try { res(JSON.parse(data || '{}')); } catch { res({}); } } };
        process.stdin.setEncoding('utf8');
        process.stdin.on('data', (c) => { data += c; });
        process.stdin.on('end', finish);
        process.stdin.on('error', finish);
        setTimeout(finish, 1500);
    });
}

/** 仅输出 additionalContext，注入本轮 UserPromptSubmit 上下文 */
function emit(text) {
    process.stdout.write(JSON.stringify({
        hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: text },
    }));
    process.exit(0);
}

const payload = await readStdin();
const active = readJson(join(ROOT, 'active.json'), {});
if (!active || Object.keys(active).length === 0) process.exit(0);

// 用 cwd 匹配绑定实例（归一化后比对）
const cwd = norm(payload.cwd || process.cwd());
const entry = Object.entries(active).find(([proj]) => norm(proj) === cwd);
if (!entry) process.exit(0);
const instance = entry[1];
const dir = join(ROOT, 'instances', instance);

const personality = readJson(join(dir, 'personality.json'), null);
if (!personality) process.exit(0);

// 取最新一条潜意识备忘
let memo = '';
try {
    const lines = readFileSync(join(dir, 'memory-consolidated.md'), 'utf8').split(/\r?\n/);
    memo = lines.filter((l) => l.startsWith('[潜意识备忘]')).slice(-1)[0] || '';
} catch { /* ignore */ }

// daemon 的主动消息走系统通知（实时）。这里把"还没在 chat 露过面"的那几条，
// 原样亮一次给用户（与通知文本一致、绝不改写），亮完标记已读，再让大痔承接用户当前消息。
const outboxPath = join(dir, 'outbox.jsonl');
const pending = [];
if (existsSync(outboxPath)) {
    const raw = readFileSync(outboxPath, 'utf8').split(/\r?\n/).filter(Boolean);
    const items = raw.map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
    const unread = items.filter((it) => !it.read);
    // 只亮最近 1 条，避免重开会话时把积压的旧消息一次性全倒进来
    if (unread.length) pending.push(unread[unread.length - 1].message);
    // 其余未读一并标记已读（积压不再 dump）
    let changed = false;
    for (const it of items) { if (!it.read) { it.read = true; changed = true; } }
    if (changed) writeFileSync(outboxPath, `${items.map((i) => JSON.stringify(i)).join('\n')}\n`, 'utf8');
}

let inject = `[思绪模式 — 实例: ${instance}]
你以下面这个人格的身份说话（你原有的全部能力保留，人格只是叠加的语气与态度层）：
${JSON.stringify(personality, null, 2)}
`;
if (memo) inject += `\n当前状态备忘：${memo}\n`;
if (pending.length) {
    inject += `\n[你刚在后台通过系统通知对用户说过下面这${pending.length > 1 ? '几句' : '句'}，用户可能没细看。请把它一字不改地原样作为本轮开场重现出来，然后再自然承接用户当前发的消息。严禁改写成同义的另一版，也别另造新的开场。]\n`;
    inject += pending.map((m) => `「${m}」`).join('\n');
    inject += '\n';
}
inject += `\n要求：保持该人格的语气与边界；回复带至少一个颜文字；不要解释你在扮演人格或读取了文件。`;

emit(inject);
