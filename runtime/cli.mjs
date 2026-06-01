#!/usr/bin/env node
// 思绪运行时 — 命令行入口
// 用法：
//   node runtime/cli.mjs start <实例> [--backend=auto|claude|cursor|api] [--cwd=路径]
//   node runtime/cli.mjs stop <实例>
//   node runtime/cli.mjs status
//   node runtime/cli.mjs once <实例> [--backend=...] [--kind=active|subconscious]
//   node runtime/cli.mjs ping <实例>      # 标记用户刚刚活跃（重置未回复计数）

import { ensureInstanceFiles, readJson, writeJson, listInstances, tailJsonl } from './core/store.mjs';
import { initInstance } from './core/onboarding.mjs';
import { DAEMON_STATE_FILE } from './core/paths.mjs';
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
            const instance = positional[0];
            if (!instance) throw new Error('用法: ping <实例>');
            const p = ensureInstanceFiles(instance);
            const loopState = readJson(p.loopState, {});
            loopState.lastUserAt = Date.now();
            loopState.consecutiveNoReply = 0;
            writeJson(p.loopState, loopState);
            console.log(`已标记 ${instance} 的用户活跃，重置未回复计数。`);
            break;
        }

        default:
            console.log(`思绪运行时 CLI
  init <实例>                                                    初始化实例（默认画像/人格/权限）
  start <实例> [--backend=auto|claude|cursor|api] [--cwd=路径]   启动常驻 daemon
  stop <实例>                                                    停止 daemon
  status                                                         查看后端与实例状态
  once <实例> [--backend=...] [--kind=active|subconscious]       手动跑一次（测试用）
  outbox <实例> [--n=10]                                         查看最近的主动消息记录
  ping <实例>                                                    标记用户刚刚活跃`);
    }
}

main().catch((err) => {
    console.error(`错误：${err.message}`);
    process.exit(1);
});
