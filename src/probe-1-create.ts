/**
 * Probe 1 — 用 SDK 创建一个带独特标志的 local agent
 *
 * 目的:
 *   - 验证 SDK 自身能正确创建 local agent (baseline)
 *   - 给后续 probe 提供一个可识别的标志物 (PROBE_<timestamp>)
 *
 * 输出:
 *   - 写入 .probe-state.json,记录 agentId / marker / cwd
 */

import { Agent } from '@cursor/sdk';
import { writeFile } from 'node:fs/promises';

const MARKER = `PROBE_${Date.now()}`;

async function main(): Promise<void> {
    if (!process.env.CURSOR_API_KEY) {
        console.error('请先设置环境变量 CURSOR_API_KEY');
        process.exit(1);
    }

    const cwd = process.cwd();
    const agent = await Agent.create({
        apiKey: process.env.CURSOR_API_KEY,
        model: { id: 'composer-2' },
        agentId: `probe-${Date.now()}`,
        name: `Interop Probe ${MARKER}`,
        local: { cwd },
    });

    try {
        console.log('[创建成功]');
        console.log(`  agentId    = ${agent.agentId}`);
        console.log(`  name       = Interop Probe ${MARKER}`);
        console.log(`  marker     = ${MARKER}`);
        console.log(`  cwd        = ${cwd}`);
        console.log('');

        const run = await agent.send(
            `请仅用一句中文回复: "我已收到 marker ${MARKER}"`,
        );
        const result = await run.wait();
        console.log(`[首条回复 status] ${result.status}`);
        console.log(`[首条回复 result] ${result.result}`);
        console.log('');

        await writeFile(
            '.probe-state.json',
            JSON.stringify(
                {
                    agentId: agent.agentId,
                    marker: MARKER,
                    cwd,
                    createdAt: new Date().toISOString(),
                },
                null,
                4,
            ),
            'utf-8',
        );
        console.log('已写入 .probe-state.json,后续 probe 会读这里');
    } finally {
        await agent[Symbol.asyncDispose]();
    }
}

main().catch((err) => {
    console.error('Probe 1 失败:', err);
    process.exit(1);
});
