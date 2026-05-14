/**
 * Probe 2 — 用 SDK 列出本地 agent
 *
 * 目的:
 *   - Q1 的一半: SDK 自查能否看到 probe-1 创建的 agent
 *   - Q3a 的探针: 也能看到 IDE / CLI 创建的 agent (互通才行)
 *
 * 用法:
 *   npm run probe:list                    # 找 .probe-state.json 里的 marker
 *   npm run probe:list -- --keyword=foo   # 自定义关键字 (例如 IDE_PROBE_xxx)
 */

import { Agent } from '@cursor/sdk';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';

interface ProbeState {
    agentId: string;
    marker: string;
    cwd: string;
}

async function loadStateOrNull(): Promise<ProbeState | null> {
    if (!existsSync('.probe-state.json')) return null;
    return JSON.parse(await readFile('.probe-state.json', 'utf-8'));
}

function parseKeyword(): string | null {
    const arg = process.argv.find((a) => a.startsWith('--keyword='));
    return arg ? arg.slice('--keyword='.length) : null;
}

async function main(): Promise<void> {
    if (!process.env.CURSOR_API_KEY) {
        console.error('请先设置环境变量 CURSOR_API_KEY');
        process.exit(1);
    }

    const overrideKeyword = parseKeyword();
    const state = await loadStateOrNull();
    const keyword = overrideKeyword ?? state?.marker ?? null;
    const cwd = state?.cwd ?? process.cwd();

    console.log(`正在列出本地 agent`);
    console.log(`  cwd     = ${cwd}`);
    console.log(`  keyword = ${keyword ?? '(无,只列出全部)'}`);
    console.log('');

    const { items, nextCursor } = await Agent.list({
        runtime: 'local',
        cwd,
        limit: 50,
    });

    console.log(`SDK 共列出 ${items.length} 个本地 agent` +
        (nextCursor ? ` (还有更多, nextCursor=${nextCursor})` : ''));
    console.log('-'.repeat(80));

    let matched = 0;
    for (const a of items) {
        const isHit = keyword !== null && (
            a.name?.includes(keyword) ||
            a.agentId.includes(keyword) ||
            (a.summary ?? '').includes(keyword)
        );
        if (isHit) matched += 1;
        const flag = isHit ? '  ★ MATCH' : '';
        const time = a.lastModified ? new Date(a.lastModified).toISOString() : '?';
        console.log(`  ${a.agentId}`);
        console.log(`    name : ${a.name}${flag}`);
        console.log(`    time : ${time}`);
        if (a.summary) console.log(`    sum  : ${a.summary.slice(0, 80)}`);
        console.log('');
    }

    console.log('-'.repeat(80));
    if (keyword) {
        console.log(`匹配 keyword "${keyword}" 的 agent 数量: ${matched}`);
        if (matched === 0) {
            console.log('  ✗ 没找到。如果你期望找到,可能需要确认 cwd 是否一致。');
        } else {
            console.log('  ★ 找到了,SDK list 可见性 OK');
        }
    }
}

main().catch((err) => {
    console.error('Probe 2 失败:', err);
    process.exit(1);
});
