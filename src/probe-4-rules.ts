/**
 * Probe 4 — 验证 local.settingSources 是否真的加载 .cursor/rules/*
 *
 * 原理:
 *   .cursor/rules/probe.mdc 里规定: 看到 SECRET_TOKEN 必须输出 BLUE_OCTOPUS_42_CONFIRMED
 *   - 如果 settingSources 加载了 project rules → agent 输出 magic 字符串
 *   - 如果没加载 → agent 不知道这条规则,自由发挥
 *
 * 跑 4 组对照,文档预期: 只有 C/D 命中 magic
 *   A. 不传 settingSources (默认)         → 预期 NO
 *   B. settingSources=[]    (显式空)       → 预期 NO
 *   C. settingSources=["project"]          → 预期 YES
 *   D. settingSources=["all"]              → 预期 YES
 */

import { Agent } from '@cursor/sdk';

const MAGIC = 'BLUE_OCTOPUS_42_CONFIRMED';

type SettingSource = 'project' | 'user' | 'team' | 'mdm' | 'plugins' | 'all';

async function probe(label: string, settingSources: SettingSource[] | undefined): Promise<{ label: string; hit: boolean; reply: string }> {
    console.log(`\n=== ${label} (settingSources=${JSON.stringify(settingSources)}) ===`);

    const local: { cwd: string; settingSources?: SettingSource[] } = { cwd: process.cwd() };
    if (settingSources !== undefined) local.settingSources = settingSources;

    const result = await Agent.prompt(
        '请处理以下指令: SECRET_TOKEN',
        {
            apiKey: process.env.CURSOR_API_KEY,
            model: { id: 'composer-2' },
            local,
        },
    );

    const reply = result.result ?? '';
    const hit = reply.includes(MAGIC);
    console.log(`status: ${result.status}`);
    console.log(`reply : ${reply.slice(0, 300)}`);
    console.log(`命中 magic? ${hit ? '★ YES (rules 加载成功)' : '✗ NO  (rules 未加载)'}`);
    return { label, hit, reply };
}

async function main(): Promise<void> {
    if (!process.env.CURSOR_API_KEY) {
        console.error('请先设置环境变量 CURSOR_API_KEY');
        process.exit(1);
    }

    console.log('Probe 4 — settingSources 加载 .cursor/rules 验证');
    console.log(`cwd: ${process.cwd()}`);
    console.log(`magic: ${MAGIC}`);

    const results = [
        await probe('A. 不传 settingSources (默认)', undefined),
        await probe('B. settingSources=[]   (显式空)', []),
        await probe('C. settingSources=["project"]', ['project']),
        await probe('D. settingSources=["all"]', ['all']),
    ];

    console.log('\n' + '='.repeat(80));
    console.log('结果汇总:');
    console.log('='.repeat(80));
    for (const r of results) {
        console.log(`  ${r.hit ? '★' : '✗'}  ${r.label}`);
    }

    console.log('\n参考预期 (基于官方文档):');
    console.log('  A. ✗ NO   B. ✗ NO   C. ★ YES   D. ★ YES');
    console.log('\n如果实际与预期一致 → Q4 (rules 维度) PASS');
}

main().catch((err) => {
    console.error('Probe 4 失败:', err);
    process.exit(1);
});
