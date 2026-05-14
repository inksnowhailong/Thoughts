/**
 * Probe 3 — 用 SDK resume 一个指定 agent,看能否接管对话历史
 *
 * 目的:
 *   - Q3b: 验证 SDK resume 出来的 agent 是否真的有之前的上下文
 *
 * 用法:
 *   npm run probe:resume -- <agentId>
 *
 * 例:
 *   npm run probe:resume -- probe-1715000000000     # SDK 自己创建的
 *   npm run probe:resume -- agent-xxxxxxxx          # IDE / CLI 创建的
 */

import { Agent, CursorAgentError } from '@cursor/sdk';

async function main(): Promise<void> {
    if (!process.env.CURSOR_API_KEY) {
        console.error('请先设置环境变量 CURSOR_API_KEY');
        process.exit(1);
    }

    const targetId = process.argv[2];
    if (!targetId || targetId.startsWith('--')) {
        console.error('用法: npm run probe:resume -- <agentId>');
        process.exit(1);
    }

    console.log(`正在 resume agent: ${targetId}`);
    console.log(`cwd: ${process.cwd()}`);
    console.log('');

    const agent = await Agent.resume(targetId, {
        apiKey: process.env.CURSOR_API_KEY,
        model: { id: 'composer-2' },
        local: { cwd: process.cwd() },
    });

    try {
        console.log(`[resume 成功] agentId = ${agent.agentId}`);
        console.log('');

        const run = await agent.send(
            '我现在是从 SDK 接管你的。请用一句话告诉我: 你之前的对话最后一条用户消息是什么? 如果你完全不记得任何之前的对话,直接回答 "我没有任何之前的对话上下文"。',
        );

        const result = await run.wait();
        console.log(`[status  ] ${result.status}`);
        console.log(`[duration] ${result.durationMs}ms`);
        console.log(`[reply   ] ${result.result}`);
        console.log('');
        console.log('— 判断方法 —');
        console.log('如果回复里能复述出 IDE / CLI 里发过的消息内容 → Q3b PASS (对话历史互通)');
        console.log('如果回复说 "没有上下文" 或乱猜 → Q3b 部分/FAIL (元数据互通,对话不互通)');
    } catch (err) {
        if (err instanceof CursorAgentError) {
            console.error(`[startup-fail] code=${err.code} retryable=${err.isRetryable}`);
            console.error(`  message: ${err.message}`);
            console.error('  含义: resume 失败,可能 agentId 不存在或 cwd 不匹配');
            process.exit(2);
        }
        throw err;
    } finally {
        await agent[Symbol.asyncDispose]();
    }
}

main().catch((err) => {
    console.error('Probe 3 失败:', err);
    process.exit(1);
});
