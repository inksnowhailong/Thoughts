#!/usr/bin/env node
// 思绪运行时 — 命令行入口
// 用法：
//   node runtime/cli.mjs start <实例> [--backend=auto|claude|api] [--cwd=路径]
//   node runtime/cli.mjs stop <实例>
//   node runtime/cli.mjs status
//   node runtime/cli.mjs once <实例> [--backend=...] [--kind=active|subconscious]
//   node runtime/cli.mjs ping <实例>      # 标记用户刚刚活跃（重置未回复计数）

import { join } from 'node:path';
import { writeFileSync } from 'node:fs';
import {
    ensureInstanceFiles, readJson, writeJson, listInstances, tailJsonl, appendJsonl,
} from './core/store.mjs';
import { recordSpoken, defaultMindState } from './core/mind.mjs';
import { decideActive } from './core/decide.mjs';
import { effectiveHeat, scheduleNext, heatTier, bumpHeat } from './core/heat.mjs';
import { pickKaomoji } from './core/kaomoji.mjs';
import { notify } from './core/notify.mjs';
import { initInstance } from './core/onboarding.mjs';
import { DAEMON_STATE_FILE } from './core/paths.mjs';
import { beijingHour } from './core/clock.mjs';
import { resolveBackend, backendStatus } from './backends/index.mjs';
import { startDaemon, doActive, doSubconscious } from './daemon/daemon.mjs';

/** 探测某个 pid 是否存活（signal 0 不真正发信号，只做存在性检查） */
function isAlive(pid) {
    try {
        process.kill(pid, 0);
        return true;
    } catch (err) {
        // EPERM 表示进程存在但无权限（仍算存活）；ESRCH 表示不存在
        return err.code === 'EPERM';
    }
}

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

        case 'start': {
            const instance = positional[0];
            if (!instance) throw new Error('用法: start <实例>');
            startDaemon({ instance, backend: flags.backend || 'auto', cwd: flags.cwd || process.cwd() });
            // startDaemon 内部用定时器维持进程存活，这里不返回
            break;
        }

        case 'stop': {
            const instance = positional[0];
            if (!instance) throw new Error('用法: stop <实例>');
            const state = readJson(DAEMON_STATE_FILE, {});
            const entry = state[instance];
            if (!entry?.pid) {
                console.log(`实例 ${instance} 没有在运行的 daemon。`);
                break;
            }
            try {
                process.kill(entry.pid, 'SIGTERM');
                console.log(`已向 daemon (pid=${entry.pid}) 发送停止信号。`);
            } catch (err) {
                console.log(`进程可能已退出：${err.message}`);
            }
            // Windows 不可靠地触发 SIGTERM 自清理，这里统一主动清除记录
            delete state[instance];
            writeJson(DAEMON_STATE_FILE, state);
            break;
        }

        case 'status': {
            console.log('=== 可用后端 ===');
            for (const b of backendStatus()) {
                console.log(`  ${b.available ? '✓' : '✗'} ${b.name}${b.agentic ? ' (agentic)' : ''}`);
            }
            console.log('\n=== 实例 ===');
            const instances = listInstances();
            const running = readJson(DAEMON_STATE_FILE, {});
            let healed = false;
            if (instances.length === 0) console.log('  （暂无实例）');
            for (const name of instances) {
                let r = running[name];
                // 探活：pid 已死则清理僵尸记录（自愈）
                if (r?.pid && !isAlive(r.pid)) {
                    delete running[name];
                    healed = true;
                    r = null;
                }
                console.log(`  ${name}${r ? ` ← 运行中 (pid=${r.pid}, backend=${r.backend})` : ' ← 未运行'}`);
            }
            if (healed) writeJson(DAEMON_STATE_FILE, running);
            break;
        }

        case 'once': {
            const instance = positional[0];
            if (!instance) throw new Error('用法: once <实例>');
            const p = ensureInstanceFiles(instance);
            const agent = resolveBackend(flags.backend || 'auto');
            const cwd = flags.cwd || process.cwd();
            if (flags.kind === 'subconscious') {
                await doSubconscious(p, agent, cwd, instance);
            } else {
                await doActive(p, agent, cwd, instance);
            }
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

        case 'next': {
            // 动态计算下次主动循环间隔——用 decideActive 读当前状态现场决策，而非读静态缓存。
            const instance = positional[0];
            if (!instance) throw new Error('用法: next <实例>');
            const p = ensureInstanceFiles(instance);
            const loopState = readJson(p.loopState, {});
            const profile = readJson(p.profile, {});
            const mindState = readJson(p.mindState, defaultMindState());
            const { nextDelayMs } = decideActive(loopState, profile, mindState);
            process.stdout.write(`${JSON.stringify({ nextDelaySec: Math.round(nextDelayMs / 1000) })}\n`);
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
  init <实例>                                                    初始化实例（默认画像/人格/权限）
  start <实例> [--backend=auto|claude|api] [--cwd=路径]   启动常驻 daemon
  stop <实例>                                                    停止 daemon
  status                                                         查看后端与实例状态
  once <实例> [--backend=...] [--kind=active|subconscious]       手动跑一次（测试用）
  outbox <实例> [--n=10]                                         查看最近的主动消息记录
  ping <实例>                                                    标记用户刚刚活跃
  next <实例>                                                    会话自醒读 daemon 建议的下次间隔(JSON)`);
    }
}

main().catch((err) => {
    console.error(`错误：${err.message}`);
    process.exit(1);
});
