#!/usr/bin/env node
// 思绪 — 方案C 在线接力 hook（UserPromptSubmit，跨平台 Node 版）
// 作用：当你在「绑定了某实例的项目」里发消息时，向本轮对话上下文注入：
//   1) 该实例的人格设定 → 让本 chat 以思绪人格回应；
//   2) daemon 后台攒下的、尚未在 chat 里露过面的主动消息 → 让它的话"接力"进对话。
// 读不到绑定实例就静默退出，对其它项目零影响。

import { readFileSync } from 'node:fs';
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

// 注：daemon 的主动消息走系统通知，不再拽进 chat 复述（否则同一句话"通知一次+chat 复读一次"，
// 既像出现两次、又让用户不知如何回应）。outbox 只作记录，用 `cli outbox` 回看。
// 这里只注入人格，让 chat 里的回应以大痔身份正常承接"用户当前这条消息"。

let inject = `[思绪模式 — 实例: ${instance}]
你现在以下面这个人格的身份回应用户当前这条消息（你原有的全部能力保留，人格只是叠加的语气与态度层）：
${JSON.stringify(personality, null, 2)}
`;
if (memo) inject += `\n当前状态备忘：${memo}\n`;
inject += `\n要求：以该人格的语气与边界，自然地回应用户刚发的这条消息；回复带至少一个颜文字；不要复述你后台通知里说过的话，不要解释你在扮演人格或读取了文件。`;

emit(inject);
