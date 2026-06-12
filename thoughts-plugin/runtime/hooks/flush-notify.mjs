#!/usr/bin/env node
// 思绪 Stop hook — 回合结束后冲洗待发通知
// 配合 record-spoken --defer-notify=1 使用：主循环回合内只写 pending-notify.json，
// 本脚本在回合结束后被框架触发，把通知补发出去。
//
// 核心保证：**通知只在 chat 正文真的渲染之后才发**。
// 框架只渲染"回合最后一条纯文本消息"——若回合终止在工具调用上（正文被吞），
// 本脚本通过读 transcript 检测到这一点，直接删掉 pending 而不发通知，
// 从根上杜绝"只有系统通知、chat 里没字"的幽灵通知。
// 零依赖、毫秒级退出：无 pending 文件时什么都不做。

import { readdirSync, readFileSync, existsSync, unlinkSync, openSync, readSync, fstatSync, closeSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { notify } from '../core/notify.mjs';

const instancesDir = join(homedir(), '.thoughts', 'instances');

/** 从 stdin 读 Stop hook 的 JSON 载荷（含 transcript_path）；读不到返回 null */
function readStdinJson() {
    return new Promise((resolve) => {
        let data = '';
        const timer = setTimeout(() => resolve(null), 2000); // stdin 不来也不卡死
        process.stdin.setEncoding('utf8');
        process.stdin.on('data', (c) => { data += c; });
        process.stdin.on('end', () => {
            clearTimeout(timer);
            try { resolve(JSON.parse(data)); } catch { resolve(null); }
        });
        process.stdin.on('error', () => { clearTimeout(timer); resolve(null); });
    });
}

/** 读文件末尾最多 maxBytes 字节（transcript 可能很大，只看尾巴） */
function readTail(file, maxBytes = 262144) {
    const fd = openSync(file, 'r');
    try {
        const size = fstatSync(fd).size;
        const start = Math.max(0, size - maxBytes);
        const buf = Buffer.alloc(size - start);
        readSync(fd, buf, 0, buf.length, start);
        return buf.toString('utf8');
    } finally {
        closeSync(fd);
    }
}

/**
 * 判断回合的最后一条 assistant 消息是不是"纯文本收尾"。
 * 框架只渲染回合末尾的纯文本消息；若最后一条 assistant 消息里带 tool_use
 * （即回合终止在工具调用上），正文就被吞了，此时不应发通知。
 * 返回 true=正文已渲染 / false=正文被吞 / null=无法判断（保守放行）。
 */
function lastTurnRenderedText(transcriptPath) {
    try {
        if (!transcriptPath || !existsSync(transcriptPath)) return null;
        const lines = readTail(transcriptPath).split('\n');
        for (let i = lines.length - 1; i >= 0; i -= 1) {
            const line = lines[i].trim();
            if (!line) continue;
            let obj;
            try { obj = JSON.parse(line); } catch { continue; } // 尾部截断的首行解析失败属正常
            if (obj?.type !== 'assistant') continue;
            const content = obj.message?.content;
            if (!Array.isArray(content)) return null;
            const hasText = content.some((c) => c?.type === 'text' && c.text?.trim());
            const hasTool = content.some((c) => c?.type === 'tool_use');
            return hasText && !hasTool;
        }
        return null;
    } catch {
        return null;
    }
}

const payload = await readStdinJson();
const rendered = lastTurnRenderedText(payload?.transcript_path);

try {
    if (existsSync(instancesDir)) {
        for (const name of readdirSync(instancesDir)) {
            const pendingFile = join(instancesDir, name, 'pending-notify.json');
            if (!existsSync(pendingFile)) continue;
            try {
                const { title, kao, message } = JSON.parse(readFileSync(pendingFile, 'utf8'));
                unlinkSync(pendingFile); // 先删后发：通知失败也不会无限重发
                // rendered===false 说明正文被吞（回合终止在工具调用上）——压下通知，绝不出幽灵；
                // true / null（无法判断）才放行。
                if (message && rendered !== false) notify(title ?? name, kao ?? '', message);
            } catch {
                // 文件损坏也要清掉，避免每轮反复报错
                try { unlinkSync(pendingFile); } catch { /* 忽略 */ }
            }
        }
    }
} catch { /* hook 绝不阻塞主流程 */ }

process.exit(0);
