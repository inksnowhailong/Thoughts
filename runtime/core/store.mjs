// 思绪运行时 — 存储层
// 统一封装 JSON / JSONL / Markdown 的读写，以及实例文件的初始化。
// 所有写入都保证父目录存在，避免首次运行报错。

import {
    existsSync, mkdirSync, readFileSync, writeFileSync, appendFileSync, readdirSync,
} from 'node:fs';
import { dirname } from 'node:path';
import { ROOT, instancePaths } from './paths.mjs';
import { defaultMindState } from './mind.mjs';

/** 确保目录存在 */
export function ensureDir(path) {
    mkdirSync(path, { recursive: true });
}

/** 读取 JSON，失败时返回兜底值 */
export function readJson(path, fallback = null) {
    try {
        return JSON.parse(readFileSync(path, 'utf8'));
    } catch {
        return fallback;
    }
}

/** 写入 JSON（4 空格缩进，自动建目录） */
export function writeJson(path, value) {
    ensureDir(dirname(path));
    writeFileSync(path, `${JSON.stringify(value, null, 4)}\n`, 'utf8');
}

/** 读取纯文本，失败返回兜底值 */
export function readText(path, fallback = '') {
    try {
        return readFileSync(path, 'utf8');
    } catch {
        return fallback;
    }
}

/** 写入纯文本（自动建目录） */
export function writeText(path, value) {
    ensureDir(dirname(path));
    writeFileSync(path, value, 'utf8');
}

/** 向 JSONL 追加一条记录 */
export function appendJsonl(path, value) {
    ensureDir(dirname(path));
    appendFileSync(path, `${JSON.stringify(value)}\n`, 'utf8');
}

/** 读取 JSONL 的最后 n 行并解析 */
export function tailJsonl(path, n = 10) {
    const raw = readText(path, '');
    if (!raw) return [];
    const lines = raw.split(/\r?\n/).filter(Boolean);
    return lines.slice(-n).map((line) => {
        try {
            return JSON.parse(line);
        } catch {
            return null;
        }
    }).filter(Boolean);
}

/**
 * 消费 outbox 中的未读主动消息：取"最近 1 条未读"返回供前台浮现，
 * 同时把所有未读标记已读并回写——避免重开会话/再次拉取时重复浮现，也不一次性倒出积压。
 * chat 注入 hook 与会话自醒 pull 共用此逻辑，保证两条浮现路径行为一致（单一真相）。
 * @param {string} outboxPath outbox.jsonl 路径
 * @returns {{ message: string|null, cleared: number }} 最近一条未读(无则 null) 与本次清掉的未读条数
 */
export function consumeUnreadOutbox(outboxPath) {
    if (!existsSync(outboxPath)) return { message: null, cleared: 0 };
    const items = readText(outboxPath, '').split(/\r?\n/).filter(Boolean)
        .map((line) => { try { return JSON.parse(line); } catch { return null; } })
        .filter(Boolean);
    const unread = items.filter((it) => !it.read);
    const message = unread.length ? unread[unread.length - 1].message : null;
    let cleared = 0;
    for (const it of items) { if (!it.read) { it.read = true; cleared += 1; } }
    if (cleared) writeText(outboxPath, `${items.map((it) => JSON.stringify(it)).join('\n')}\n`);
    return { message, cleared };
}

/**
 * 列出所有已存在的实例名。
 */
export function listInstances() {
    const dir = `${ROOT}/instances`;
    if (!existsSync(dir)) return [];
    try {
        return readdirSync(dir, { withFileTypes: true })
            .filter((d) => d.isDirectory())
            .map((d) => d.name);
    } catch {
        return [];
    }
}

/**
 * 确保实例的基础文件存在，不存在则用默认值创建。
 * 返回该实例的全部路径。
 * @param {string} instance 实例名
 */
export function ensureInstanceFiles(instance) {
    const p = instancePaths(instance);
    ensureDir(p.dir);
    if (!existsSync(p.memoryRaw)) writeText(p.memoryRaw, '# 思绪记忆 - 原始\n\n');
    if (!existsSync(p.memoryConsolidated)) writeText(p.memoryConsolidated, '# 思绪记忆 - 整理\n\n');
    if (!existsSync(p.loopState)) {
        writeJson(p.loopState, {
            lastUserAt: 0,
            lastActiveAt: 0,
            consecutiveNoReply: 0,
            activeDelayMs: 15 * 60 * 1000,
            subconsciousDelayMs: 20 * 60 * 1000,
        });
    }
    if (!existsSync(p.mindState)) writeJson(p.mindState, defaultMindState());
    return p;
}
