#!/usr/bin/env node
// 思绪运行时 — 命令行入口
// 用法：
//   node runtime/cli.mjs status
//   node runtime/cli.mjs once <实例> --kind=subconscious   # 跑一次潜意识消化（挂在宿主 cron 上）
//   node runtime/cli.mjs gate <实例>      # chat 内主动开口的闸门（挂在宿主心跳 cron 上）
//   node runtime/cli.mjs ping <实例>      # 标记用户刚刚活跃（拉热度、把下次开口拉近）

import { join } from 'node:path';
import { writeFileSync } from 'node:fs';
import {
    ensureInstanceFiles, readJson, writeJson, listInstances, tailJsonl, appendJsonl,
} from './core/store.mjs';
import { recordSpoken, defaultMindState } from './core/mind.mjs';
import { effectiveHeat, scheduleNext, heatTier, bumpHeat } from './core/heat.mjs';
import { pickKaomoji } from './core/kaomoji.mjs';
import { notify } from './core/notify.mjs';
import { initInstance } from './core/onboarding.mjs';
import { beijingHour } from './core/clock.mjs';
import { resolveBackend, backendStatus } from './backends/index.mjs';
import { doSubconscious } from './core/subconscious.mjs';

/** 从 stdin 读全部输入（无管道/TTY 时立即返回空串，不阻塞） */
function readStdin() {
    return new Promise((resolve) => {
        if (process.stdin.isTTY) { resolve(''); return; }
        let data = '';
        process.stdin.setEncoding('utf8');
        process.stdin.on('data', (c) => { data += c; });
        process.stdin.on('end', () => resolve(data.trim()));
        process.stdin.on('error', () => resolve(data.trim()));
    });
}

/** 把 --key=value 形式的参数解析成对象 */
function parseFlags(argv) {
    const flags = {};
    for (const a of argv) {
        const m = a.match(/^--([^=]+)=(.*)$/);
        if (m) flags[m[1]] = m[2];
    }
    return flags;
}

const [command, ...rest] = process.argv.slice(2);
const positional = rest.filter((a) => !a.startsWith('--'));
const flags = parseFlags(rest);

async function main() {
    switch (command) {
        case 'init': {
            const instance = positional[0];
            if (!instance) throw new Error('用法: init <实例>');
            const { created } = initInstance(instance);
            console.log(created.length
                ? `已初始化实例 ${instance}，创建：${created.join('、')}`
                : `实例 ${instance} 已存在，未覆盖任何文件。`);
            console.log(`下一步：node runtime/cli.mjs start ${instance}`);
            break;
        }

        case 'status': {
            console.log('=== 可用后端 ===');
            for (const b of backendStatus()) {
                console.log(`  ${b.available ? '✓' : '✗'} ${b.name}${b.agentic ? ' (agentic)' : ''}`);
            }
            console.log('\n=== 实例 ===');
            const instances = listInstances();
            if (instances.length === 0) console.log('  （暂无实例）');
            for (const name of instances) console.log(`  ${name}`);
            break;
        }

        case 'once': {
            // 只跑潜意识消化（脱离 chat 的 daemon 已移除，once 不再有"主动开口"分支）。
            const instance = positional[0];
            if (!instance) throw new Error('用法: once <实例> --kind=subconscious');
            const p = ensureInstanceFiles(instance);
            const agent = resolveBackend(flags.backend || 'auto');
            const cwd = flags.cwd || process.cwd();
            await doSubconscious(p, agent, cwd, instance);
            break;
        }

        case 'outbox': {
            const instance = positional[0];
            if (!instance) throw new Error('用法: outbox <实例>');
            const p = ensureInstanceFiles(instance);
            const n = Number(flags.n) || 10;
            const items = tailJsonl(p.outbox, n);
            if (items.length === 0) {
                console.log(`${instance} 还没说过话。`);
                break;
            }
            console.log(`=== ${instance} 最近 ${items.length} 条主动消息 ===`);
            for (const it of items) {
                const t = new Date(it.time).toLocaleString();
                console.log(`[${t}]${it.read ? '' : ' (未读)'} ${it.message}`);
            }
            break;
        }

        case 'ping': {
            // 标记用户活跃 + 拉热度。除 hook 外也作为心跳轮的"对账自愈"入口：
            // hook 漏听用户消息时（实测会发生），由看得见对话的心跳轮补打 ping 对齐状态。
            const instance = positional[0];
            if (!instance) throw new Error('用法: ping <实例>');
            const p = ensureInstanceFiles(instance);
            const loopState = readJson(p.loopState, {});
            loopState.lastUserAt = Date.now();
            loopState.consecutiveNoReply = 0;
            bumpHeat(loopState); // 加热并把 nextSpeakAt 只拉近不推远
            writeJson(p.loopState, loopState);
            console.log(`已标记 ${instance} 的用户活跃：heat=${loopState.heat.toFixed(2)}，下次开口已拉近。`);
            break;
        }

        case 'gate': {
            // Cron 在 chat 内主动开口的"该不该说"闸门——红线只剩两条：
            //   深夜静默 / 还没到 heat 模型排定的 nextSpeakAt（唯一限速来源）。
            //   绝不因"用户刚打字"而哑——边聊边主动恰恰是要的。
            //   输出 SPEAK <hot|warm|cold>（热度分层，指导内容贴话题还是聊自己的）
            //   或 SILENT <原因> <颜文字>——静默跳的"滴答声"按状态换脸：深夜打盹、凉了发呆、热着候场，
            //   心跳轮原样回显这个颜文字，时间线不再是一排死点。
            const instance = positional[0];
            if (!instance) throw new Error('用法: gate <实例>');
            const p = ensureInstanceFiles(instance);
            const loopState = readJson(p.loopState, {});
            const profile = readJson(p.profile, {});
            // 滴答脸 = 处境 × 心情（personaState 三轴），与开口语气同源，见 core/kaomoji.mjs
            const personaState = readJson(p.mindState, {})?.personaState || {};
            const hour = beijingHour(); // 作息红线按北京时间
            const [qs, qe] = profile?.habits?.quietHours || [23, 7];
            const quiet = qs <= qe ? (hour >= qs && hour < qe) : (hour >= qs || hour < qe);
            if (quiet) { console.log(`SILENT quiet_hour ${pickKaomoji('quiet', personaState)}`); break; }
            const now = Date.now();
            const nextSpeakAt = Number(loopState.nextSpeakAt || 0);
            if (now < nextSpeakAt) {
                const tier = heatTier(effectiveHeat(loopState, now));
                console.log(`SILENT not_due ${pickKaomoji(tier, personaState)}`);
                break;
            }
            console.log(`SPEAK ${heatTier(effectiveHeat(loopState, now))}`);
            break;
        }

        case 'record-spoken': {
            // Cron 在 chat 说完一条后回写状态：更新去重/模式多样性、记 activity、刷新 lastActiveAt。
            // 消息文本通过 stdin 传入，避免命令行转义中文/引号。
            const instance = positional[0];
            const mode = positional[1] || 'casual';
            if (!instance) throw new Error('用法: record-spoken <实例> <mode>  (消息走 stdin)');
            const message = await readStdin();
            const p = ensureInstanceFiles(instance);
            const mindState = readJson(p.mindState, defaultMindState());
            recordSpoken(mindState, mode, message);
            writeJson(p.mindState, mindState);
            appendJsonl(p.activityLog, {
                time: new Date().toISOString(), action: 'chat', mode, topic: message.slice(0, 60), via: 'cron',
            });
            const loopState = readJson(p.loopState, {});
            loopState.lastActiveAt = Date.now();
            // 说完话按此刻有效热度排下一次开口时间（说话不加热；吞掉的回合走不到这里，自然会重试）
            scheduleNext(loopState);
            writeJson(p.loopState, loopState);
            // 同一文本同时弹系统通知：chat 与通知是一个脑子的两个窗口，不看 chat 时也被叫到
            const persona = readJson(p.personality, {});
            if (persona.useNotification !== false && message) {
                const kao = persona.kaomoji
                    ?? (Array.isArray(persona.kaomojiPreference) ? persona.kaomojiPreference[0] : null)
                    ?? '(´･ᴗ･`)';
                if (flags['defer-notify']) {
                    // 延迟通知：只落一个 pending 文件，由回合结束后的 Stop hook（flush-notify.mjs）冲洗。
                    // 目的：保证 chat 正文先渲染、通知后到——根除"被打断时通知已发、正文没出"的幽灵通知。
                    writeFileSync(join(p.dir, 'pending-notify.json'), JSON.stringify({
                        title: persona.name ?? instance, kao, message,
                    }), 'utf8');
                } else {
                    notify(persona.name ?? instance, kao, message);
                }
            }
            console.log('recorded');
            break;
        }

        default:
            console.log(`思绪运行时 CLI
  init <实例>                          初始化实例（默认画像/人格/权限）
  status                               查看后端与实例状态
  once <实例> --kind=subconscious      跑一次潜意识消化（挂在宿主 cron 上）
  gate <实例>                          chat 内主动开口的闸门（挂在宿主心跳 cron 上）
  ping <实例>                          标记用户刚刚活跃（拉热度、把下次开口拉近）
  record-spoken <实例> <mode>          说完一条后回写状态（消息走 stdin）
  outbox <实例> [--n=10]               查看最近的主动消息记录`);
    }
}

main().catch((err) => {
    console.error(`错误：${err.message}`);
    process.exit(1);
});
