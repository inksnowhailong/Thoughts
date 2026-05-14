/**
 * Probe 5 (可选) — 验证 settingSources 是否加载用户 / 项目级 MCP
 *
 * 原理:
 *   你的 ~/.cursor/mcp.json 里如果配过 MCP server (比如 playwright),
 *   - settingSources=['user'] 应该让 agent 看到这些 MCP 工具
 *   - settingSources=[]       agent 应该看不到
 *
 *   通过让 agent "列出当前可用的所有 MCP 工具",对比两次的清单差异。
 *
 * 注意:
 *   - 如果你 ~/.cursor/mcp.json 没配 MCP,这个 probe 没什么意义
 *   - 模型有时会瞎编工具名,所以输出仅供肉眼判断,不要做严格断言
 */

import { Agent } from '@cursor/sdk';

type SettingSource = 'project' | 'user' | 'team' | 'mdm' | 'plugins' | 'all';

const PROMPT = `请列出你**当前实际可用**的所有 MCP 工具名 (注意:不是你"听说过"的,是你"现在就能调用"的)。
- 一行一个工具名
- 只输出工具名,不要描述、不要 markdown
- 如果一个 MCP 工具都没有,就回答: NO_MCP_TOOLS_AVAILABLE`;

async function probe(label: string, settingSources: SettingSource[] | undefined): Promise<void> {
    console.log(`\n=== ${label} (settingSources=${JSON.stringify(settingSources)}) ===`);

    const local: { cwd: string; settingSources?: SettingSource[] } = { cwd: process.cwd() };
    if (settingSources !== undefined) local.settingSources = settingSources;

    const result = await Agent.prompt(PROMPT, {
        apiKey: process.env.CURSOR_API_KEY,
        model: { id: 'composer-2' },
        local,
    });

    console.log(`status: ${result.status}`);
    console.log(`reply :`);
    console.log(result.result ?? '(空)');
}

async function main(): Promise<void> {
    if (!process.env.CURSOR_API_KEY) {
        console.error('请先设置环境变量 CURSOR_API_KEY');
        process.exit(1);
    }

    console.log('Probe 5 — settingSources 加载 MCP 验证');
    console.log(`cwd: ${process.cwd()}`);

    await probe('A. settingSources=[] (无任何源)', []);
    await probe('B. settingSources=["user"]', ['user']);
    await probe('C. settingSources=["all"]', ['all']);

    console.log('\n' + '='.repeat(80));
    console.log('判断方法: 对比三次输出的工具清单差异');
    console.log('  - A 不应该有任何 user/project MCP 工具');
    console.log('  - B/C 如果你 ~/.cursor/mcp.json 有配 MCP,应该出现对应工具');
    console.log('='.repeat(80));
}

main().catch((err) => {
    console.error('Probe 5 失败:', err);
    process.exit(1);
});
