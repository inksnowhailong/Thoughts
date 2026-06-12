// 思绪运行时 — 潜意识运行器
// 由 cli `once <实例> --kind=subconscious` 触发（通常挂在宿主的 */21 cron 上）：
//   到点整理记忆 / 演化散文画像。agentic 后端自行读写文件，非 agentic 后端由这里喂数据并落盘。
// 这是「脱离 chat 的常驻 daemon」被砍掉后唯一保留的后台职责——纯粹的记忆消化，不再自己排节奏。

import { appendFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { LOG_DIR } from './paths.mjs';
import {
    readJson, writeJson, readText, writeText, appendJsonl,
} from './store.mjs';
import { decideSubconscious } from './decide.mjs';
import { buildSubconsciousPrompt } from './prompts.mjs';

/** 潜意识用的模型：结构化归纳/推断，Sonnet 足够，不必上顶配 */
const SUBCONSCIOUS_MODEL = 'claude-sonnet-4-6';

/** 写一条潜意识日志（落盘 + 控制台） */
export function log(instance, event, detail = {}) {
    const line = { time: new Date().toISOString(), event, ...detail };
    try {
        mkdirSync(LOG_DIR, { recursive: true });
        appendFileSync(join(LOG_DIR, `subconscious-${instance}.jsonl`), `${JSON.stringify(line)}\n`, 'utf8');
    } catch {
        // 日志失败绝不能中断消化
    }
    console.log(`[思绪:${instance}] ${event}`, Object.keys(detail).length ? JSON.stringify(detail) : '');
}

/** raw 里是否有可消化的新料（"- " 开头的条目）；没有就别劳师动众跑潜意识 */
function hasNewRawMemory(path) {
    return readText(path, '').split(/\r?\n/).some((l) => l.trimStart().startsWith('- '));
}

/**
 * 执行一次潜意识循环：消化 raw → 原地重炼散文画像。
 * @param {object} p 实例路径表
 * @param {object} agent 后端
 * @param {string} cwd 项目目录（未用，保留签名一致）
 * @param {string} instance 实例名
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
            // 无工具的后端：喂数据 + 落盘
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
