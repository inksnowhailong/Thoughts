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
import { decideActive, decideSubconscious, hardGate } from '../core/decide.mjs';
import { defaultMindState, recordSpoken, selectMode, MODES } from '../core/mind.mjs';
import { resolveBackend } from '../backends/index.mjs';
import { buildActivePrompt, buildSubconsciousPrompt, buildDecisionPrompt } from './prompts.mjs';

/** 决策层用的便宜模型：只做"该不该开口"的判断，省 token */
const DECISION_MODEL = 'claude-haiku-4-5';

/** 潜意识用的模型：结构化归纳/推断，Sonnet 足够，不必上顶配 */
const SUBCONSCIOUS_MODEL = 'claude-sonnet-4-6';

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

/** raw 里是否有可消化的新料（"- " 开头的条目）；没有就别劳师动众跑潜意识 */
function hasNewRawMemory(path) {
    return readText(path, '').split(/\r?\n/).some((l) => l.trimStart().startsWith('- '));
}

/** 从人格里取一个颜文字，没有就给个默认 */
function kaomojiOf(personality) {
    return personality?.kaomoji
        ?? (Array.isArray(personality?.kaomojiPreference) ? personality.kaomojiPreference[0] : null)
        ?? '(´･ᴗ･`)';
}

/** 把决策模型返回的文本里那段 JSON 抠出来并校验，失败返回 null */
function parseDecision(text) {
    try {
        const m = String(text || '').match(/\{[\s\S]*\}/);
        if (!m) return null;
        const o = JSON.parse(m[0]);
        if (typeof o.act !== 'boolean') return null;
        return o;
    } catch {
        return null;
    }
}

/** 把模型给的"分钟"限幅到 [10, 120] 并转毫秒；非法值给 15 分钟兜底 */
function clampDelayMs(minutes) {
    const n = Number(minutes);
    if (!Number.isFinite(n)) return 15 * 60 * 1000;
    return Math.max(10, Math.min(120, Math.round(n))) * 60 * 1000;
}

/**
 * 双层主动决策：硬闸门(红线纯代码) → 便宜模型软判断 → 现有纯代码兜底。
 * 这是 Fix 2 的核心：把硬编码的 5 档间隔换成"懂分寸的现场判断"，且全程可降级。
 * @param {object} ctx
 * @param {object} ctx.loopState 循环状态
 * @param {object} ctx.profile 用户画像
 * @param {object} ctx.mindState 心智状态
 * @param {object} ctx.env 环境快照
 * @param {object} ctx.agent 后端
 * @param {string} ctx.rawExcerpt 用户最近真实发言片段
 * @param {string} ctx.userPortrait 散文体用户画像(喂软判断)
 * @returns {Promise<{act:boolean,reason:string,nextDelayMs:number,mode:string,source:string}>}
 */
async function decideActiveSmart({
    loopState, profile, mindState, env, agent, rawExcerpt, userPortrait = '',
}) {
    // Layer A：红线命中直接返回，0 成本不调模型（quietHours 仍来自 profile 机器配置）
    const gated = hardGate(loopState, profile);
    if (gated) return { ...gated, source: 'gate' };

    // Layer B：便宜模型软判断
    try {
        const prompt = buildDecisionPrompt({
            userPortrait, env, mindState, recentMessages: mindState.recentMessages || [], rawExcerpt, loopState,
        });
        const result = await agent.run({ prompt, model: DECISION_MODEL, timeoutMs: 60000 });
        const parsed = result.ok && parseDecision(result.text);
        if (parsed) {
            const mode = parsed.act
                ? (MODES.includes(parsed.mode) ? parsed.mode : selectMode(mindState))
                : 'quiet';
            return {
                act: parsed.act,
                mode,
                nextDelayMs: clampDelayMs(parsed.nextDelayMinutes),
                reason: parsed.reason || 'llm',
                source: 'llm',
            };
        }
    } catch {
        // 落到纯代码兜底
    }

    // Fallback：现有 INTERVAL 决策（优雅降级，守零依赖底线）
    return { ...decideActive(loopState, profile, mindState), source: 'fallback' };
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
    const profile = readJson(p.profile, {}); // 仅供 hardGate 读 quietHours 等机器配置
    const personality = readJson(p.personality, {});
    const mindState = readJson(p.mindState, defaultMindState());
    // 散文体画像：人格 + 用户，是"说什么/对谁说"的命脉，取代旧的 JSON 字段堆砌
    const personaText = readText(p.persona, '');
    const userPortrait = readText(p.userPortrait, '');
    // 环境快照上提：既喂决策层(该不该开口)，也喂生成层(说什么)
    const env = senseEnvironment(readJson(p.permissions, {}), { cwd });
    // 用户最近真说过的话：决策层据此判断状态，生成层据此"接话"而非自顾自抛冷知识
    const userRecent = memoryExcerpt(p.memoryRaw, 20);
    const decision = await decideActiveSmart({
        loopState, profile, mindState, env, agent, rawExcerpt: userRecent, userPortrait,
    });
    log(instance, 'active_decide', decision);

    if (decision.act) {
        try {
            const prompt = buildActivePrompt({
                personality, personaText, userPortrait, memoryExcerpt: memoryExcerpt(p.memoryConsolidated), env, mode: decision.mode, mindState, userRecent,
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

    // 省 token 头号刀：raw 没有新料就整轮跳过——只读一次文件、0 模型调用。
    // raw 仅由用户在 chat 里说话才会填充，所以"空 raw"≈"你没说话"，没东西可消化。
    if (!hasNewRawMemory(p.memoryRaw)) {
        log(instance, 'subconscious_skipped', { reason: 'empty_raw', ...decision });
        loopState.subconsciousDelayMs = decision.nextDelayMs;
        writeJson(p.loopState, loopState);
        return decision.nextDelayMs;
    }

    log(instance, 'subconscious_run', { backend: agent.name, ...decision });

    try {
        if (agent.agentic) {
            // 有工具的后端：让它自己读写记忆/画像文件。
            // ⚠️ cwd 必须是实例目录，不能用项目 cwd——记忆文件在 ~/.thoughts/instances/<实例>/，
            //   若 cwd 指向用户项目，Claude Code 的工作区沙箱会静默拒绝写入，潜意识等于空转。
            const r = await agent.run({ prompt: buildSubconsciousPrompt({ agentic: true, paths: p }), cwd: p.dir, model: SUBCONSCIOUS_MODEL });
            if (!r?.ok) log(instance, 'subconscious_failed', { error: r?.error });
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
