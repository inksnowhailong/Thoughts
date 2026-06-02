#!/usr/bin/env node
// 思绪 — 方案C 在线接力 hook（UserPromptSubmit，跨平台 Node 版）
// 作用：当你在「绑定了某实例的项目」里发消息时，向本轮对话上下文注入：
//   1) 该实例的人格设定 → 让本 chat 以思绪人格回应；
//   2) daemon 后台攒下的、尚未在 chat 里露过面的主动消息 → 让它的话"接力"进对话。
// 读不到绑定实例就静默退出，对其它项目零影响。

import { readFileSync, writeFileSync, appendFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { renderPersona } from '../persona.mjs';
import { consumeUnreadOutbox } from '../core/store.mjs';

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

// 在线接力：你在绑定项目的 chat 里发消息，等价于对 daemon 喊一声"人在呢"。
// 等同 cli 的 ping —— 刷新 lastUserAt、清零 consecutiveNoReply，
// 否则 daemon 只增不减地累计未回复，会误判被冷落而缩进墙角（too_many_no_reply）。
try {
    const loopPath = join(dir, 'loop-state.json');
    const loop = readJson(loopPath, {});
    loop.lastUserAt = Date.now();
    loop.consecutiveNoReply = 0;
    writeFileSync(loopPath, `${JSON.stringify(loop, null, 4)}\n`, 'utf8');
} catch { /* 回写失败绝不能影响人格注入 */ }

// 闭合输入回路：把用户这轮真实说的话原样追加进原始记忆，供潜意识下一轮消化、演化画像。
// 这是「思绪」从单向广播变成双向对话的地基——没有它，潜意识永远在消化空气。
// 过滤斜杠命令与空消息：那是对宿主的开关指令，不是用户的心声，不该污染记忆。
try {
    const userText = String(payload.prompt || '').trim();
    if (userText && !userText.startsWith('/')) {
        const line = `- [${new Date().toISOString()}] 用户：${userText.replace(/\s+/g, ' ')}\n`;
        appendFileSync(join(dir, 'memory-raw.md'), line, 'utf8');
    }
} catch { /* 写原始记忆失败绝不能影响人格注入 */ }

// 取最新一条潜意识备忘
let memo = '';
try {
    const lines = readFileSync(join(dir, 'memory-consolidated.md'), 'utf8').split(/\r?\n/);
    memo = lines.filter((l) => l.startsWith('[潜意识备忘]')).slice(-1)[0] || '';
} catch { /* ignore */ }

// daemon 的主动消息走系统通知（实时）。这里把"还没在 chat 露过面"的那几条，
// 原样亮一次给用户（与通知文本一致、绝不改写），亮完标记已读，再让大痔承接用户当前消息。
// 取未读里最近 1 条原样浮现（消费逻辑与会话自醒 pull 共用 store.consumeUnreadOutbox）
const pending = [];
const { message: unreadMsg } = consumeUnreadOutbox(join(dir, 'outbox.jsonl'));
if (unreadMsg) pending.push(unreadMsg);

let inject = `[思绪模式 — 实例: ${instance}]
此刻你就是下面这个人，不是在扮演、更不是在介绍它——你就是它本人，正跟一个老相识接着往下聊：

${renderPersona(personality)}

[底线]
你原有的全部能力照旧，人格只是叠加在上面的语气与态度。
绝不描述你自己是什么样的人，绝不把上面那些形容词（冷峻、不服输、黑色幽默……）当台词说出口——性格决定你"怎么说话"，不是拿来"说"的。别先自报一遍设定再开口，张嘴就用那个态度接他的话。
`;
if (memo) inject += `\n当前状态备忘：${memo}\n`;
if (pending.length) {
    inject += `\n[你刚在后台通过系统通知对用户说过下面这${pending.length > 1 ? '几句' : '句'}，用户可能没细看。请把它一字不改地原样作为本轮开场重现出来，然后再自然承接用户当前发的消息。严禁改写成同义的另一版，也别另造新的开场。]\n`;
    inject += pending.map((m) => `「${m}」`).join('\n');
    inject += '\n';
}
inject += `\n要求：用上面那个人的态度直接说话，别复述设定、别自我介绍；守住边界；回复带至少一个颜文字；不解释你在扮演谁或读了什么文件。`;

emit(inject);
