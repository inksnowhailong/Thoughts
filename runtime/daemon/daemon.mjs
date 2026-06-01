// 思绪运行时 — 常驻 Daemon（方案 B 的核心）
// 一个 daemon 进程绑定一个实例，自持两个定时器：
//   · 主动循环：到点判断该不该说 → 生成一句人格化聊天 → 系统通知
//   · 潜意识循环：到点整理记忆 / 演化画像（agentic 后端自行读写，api 后端由 daemon 落盘）
// 完全不依赖任何 AI 宿主的 hook/cron —— setTimeout 就是心跳。

import { appendFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { LOG_DIR, DAEMON_STATE_FILE } from '../core/paths.mjs';
import {
    readJson, writeJson, readText, writeText, appendJsonl, ensureInstanceFiles,
} from '../core/store.mjs';
import { senseEnvironment } from '../core/env-sense.mjs';
import { notify } from '../core/notify.mjs';
import { decideActive, decideSubconscious } from '../core/decide.mjs';
import { defaultMindState, recordSpoken } from '../core/mind.mjs';
import { resolveBackend } from '../backends/index.mjs';
import { buildActivePrompt, buildSubconsciousPrompt } from './prompts.mjs';

/** 写一条 daemon 日志（落盘 + 控制台） */
export function log(instance, event, detail = {}) {
    const line = { time: new Date().toISOString(), event, ...detail };
    try {
        mkdirSync(LOG_DIR, { recursive: true });
        appendFileSync(join(LOG_DIR, `daemon-${instance}.jsonl`), `${JSON.stringify(line)}\n`, 'utf8');
    } catch {
        // 日志失败绝不能中断循环
    }
    console.log(`[思绪:${instance}] ${event}`, Object.keys(detail).length ? JSON.stringify(detail) : '');
}

/** 取整理记忆的末尾若干行作为摘录，避免把整份记忆塞进 prompt */
function memoryExcerpt(path, maxLines = 40) {
    const lines = readText(path, '').split(/\r?\n/);
    return lines.slice(-maxLines).join('\n');
}

/** 从人格里取一个颜文字，没有就给个默认 */
function kaomojiOf(personality) {
    return personality?.kaomoji
        ?? (Array.isArray(personality?.kaomojiPreference) ? personality.kaomojiPreference[0] : null)
        ?? '(´･ᴗ･`)';
}

/**
 * 执行一次主动循环。返回本次决定的下次间隔（毫秒），供调度器使用。
 * @param {object} p 实例路径表
 * @param {object} agent 后端
 * @param {string} cwd 项目目录
 * @param {string} instance 实例名
 */
export async function doActive(p, agent, cwd, instance) {
    const loopState = readJson(p.loopState, {});
    const profile = readJson(p.profile, {});
    const personality = readJson(p.personality, {});
    const mindState = readJson(p.mindState, defaultMindState());
    const decision = decideActive(loopState, profile, mindState);
    log(instance, 'active_decide', decision);

    if (decision.act) {
        try {
            const env = senseEnvironment(readJson(p.permissions, {}), { cwd });
            const prompt = buildActivePrompt({
                personality, profile, memoryExcerpt: memoryExcerpt(p.memoryConsolidated), env, mode: decision.mode, mindState,
            });
            const result = await agent.run({ prompt, cwd });
            const message = (result.text || '').trim();
            if (result.ok && message) {
                notify(personality?.name ?? '思绪', kaomojiOf(personality), message);
                // 写入收件箱：read=false 供 chat 内 hook 浮现一次；同时是用户可回看的记录
                appendJsonl(p.outbox, { time: new Date().toISOString(), message, read: false });
                appendJsonl(p.activityLog, { time: new Date().toISOString(), action: 'chat', mode: decision.mode, topic: message.slice(0, 60) });
                // 更新心智：记录本次模式 + 留存片段供后续去重
                recordSpoken(mindState, decision.mode, message);
                writeJson(p.mindState, mindState);
                loopState.consecutiveNoReply = Number(loopState.consecutiveNoReply || 0) + 1;
                log(instance, 'active_sent', { mode: decision.mode, message });
            } else {
                log(instance, 'active_failed', { error: result.error });
            }
        } catch (err) {
            log(instance, 'active_error', { error: String(err?.message || err) });
        }
    }

    loopState.lastActiveAt = Date.now();
    loopState.activeDelayMs = decision.nextDelayMs;
    writeJson(p.loopState, loopState);
    return decision.nextDelayMs;
}

/**
 * 执行一次潜意识循环。返回下次间隔（毫秒）。
 */
export async function doSubconscious(p, agent, cwd, instance) {
    const loopState = readJson(p.loopState, {});
    const decision = decideSubconscious(loopState, readJson(p.profile, {}));
    log(instance, 'subconscious_run', { backend: agent.name, ...decision });

    try {
        if (agent.agentic) {
            // 有工具的后端：让它自己读写记忆/画像文件
            await agent.run({ prompt: buildSubconsciousPrompt({ agentic: true, paths: p }), cwd });
        } else {
            // 无工具的后端：daemon 喂数据 + 落盘
            const result = await agent.run({
                prompt: buildSubconsciousPrompt({
                    agentic: false,
                    memoryRaw: readText(p.memoryRaw, ''),
                    memoryConsolidated: readText(p.memoryConsolidated, ''),
                }),
            });
            if (result.ok && result.text) {
                writeText(p.memoryConsolidated, `${result.text}\n`);
                writeText(p.memoryRaw, '# 思绪记忆 - 原始\n\n');
            }
        }
        appendJsonl(p.activityLog, { time: new Date().toISOString(), action: 'subconscious' });
    } catch (err) {
        log(instance, 'subconscious_error', { error: String(err?.message || err) });
    }

    loopState.subconsciousDelayMs = decision.nextDelayMs;
    writeJson(p.loopState, loopState);
    return decision.nextDelayMs;
}

/**
 * 启动常驻 Daemon。
 * @param {{ instance: string, backend?: string, cwd?: string }} opts
 */
export function startDaemon({ instance, backend = 'auto', cwd = process.cwd() }) {
    const p = ensureInstanceFiles(instance);
    const agent = resolveBackend(backend);
    log(instance, 'daemon_start', { backend: agent.name, agentic: agent.agentic, cwd });

    const daemonState = readJson(DAEMON_STATE_FILE, {});
    daemonState[instance] = {
        pid: process.pid, backend: agent.name, cwd, startedAt: new Date().toISOString(),
    };
    writeJson(DAEMON_STATE_FILE, daemonState);

    let activeTimer = null;
    let subTimer = null;
    let stopped = false;

    async function activeTick() {
        if (stopped) return;
        const delay = await doActive(p, agent, cwd, instance);
        if (!stopped) activeTimer = setTimeout(activeTick, delay);
    }
    async function subTick() {
        if (stopped) return;
        const delay = await doSubconscious(p, agent, cwd, instance);
        if (!stopped) subTimer = setTimeout(subTick, delay);
    }

    function shutdown() {
        if (stopped) return;
        stopped = true;
        clearTimeout(activeTimer);
        clearTimeout(subTimer);
        const state = readJson(DAEMON_STATE_FILE, {});
        delete state[instance];
        writeJson(DAEMON_STATE_FILE, state);
        log(instance, 'daemon_stop');
        process.exit(0);
    }
    process.on('SIGINT', shutdown);
    process.on('SIGTERM', shutdown);

    // 启动后给一个较小的初始延迟先各跑一次，避免一开机就立刻打扰
    const init = readJson(p.loopState, {});
    activeTimer = setTimeout(activeTick, Math.min(Number(init.activeDelayMs) || 900000, 60000));
    subTimer = setTimeout(subTick, Math.min(Number(init.subconsciousDelayMs) || 1200000, 90000));
}
