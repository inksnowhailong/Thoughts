#!/usr/bin/env node
import {
    appendFileSync,
    copyFileSync,
    existsSync,
    mkdirSync,
    readFileSync,
    readdirSync,
    renameSync,
    statSync,
    unlinkSync,
    watch,
    writeFileSync,
} from 'node:fs';
import { execFileSync, execSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { homedir } from 'node:os';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const SCRIPT_DIR = dirname(__filename);
const ROLES_FILE = join(SCRIPT_DIR, 'team-roles.json');

function fixCursorPath(value) {
    if (typeof value !== 'string') return value;
    if (process.platform === 'win32' && /^\/[a-z]:/i.test(value)) return value.slice(1);
    return value;
}

function normalizeWorkspace(value) {
    let result = resolve(fixCursorPath(value ?? process.cwd())).replaceAll('\\', '/');
    if (process.platform === 'win32' && /^[a-z]:/.test(result)) {
        result = result[0].toUpperCase() + result.slice(1);
    }
    return result;
}

function stripBom(value) {
    return typeof value === 'string' && value.charCodeAt(0) === 0xfeff ? value.slice(1) : value;
}

function readText(path) {
    return stripBom(readFileSync(path, 'utf8'));
}

function readJson(path, fallback) {
    try {
        return JSON.parse(readText(path));
    } catch {
        return fallback;
    }
}

function ensureDir(path) {
    if (!existsSync(path)) mkdirSync(path, { recursive: true });
}

function atomicWrite(path, content) {
    ensureDir(dirname(path));
    const tmp = `${path}.${process.pid}.${Date.now()}.${randomBytes(3).toString('hex')}.tmp`;
    writeFileSync(tmp, content, 'utf8');
    renameSync(tmp, path);
}

function writeJson(path, value) {
    atomicWrite(path, `${JSON.stringify(value, null, 4)}\n`);
}

function teamDir(workspace) {
    return join(workspace, '.cursor', '.team');
}

function configPath(workspace) {
    return join(teamDir(workspace), 'config.json');
}

function rel(workspace, path) {
    return relative(workspace, path).replaceAll('\\', '/');
}

function rolesData() {
    return readJson(ROLES_FILE, { roles: [] });
}

function findRole(id) {
    return rolesData().roles.find((role) => role.id === id) ?? null;
}

function readConfig(workspace) {
    return readJson(configPath(workspace), null);
}

function requireConfig(workspace, command) {
    const config = readConfig(workspace);
    if (!config) throw new Error(`${command}: team is not initialized; run ensure-team first`);
    return config;
}

function writeConfig(workspace, config) {
    writeJson(configPath(workspace), { ...config, updatedAt: new Date().toISOString() });
}

function allAgents(config) {
    return [config.leaderName, ...(config.subs ?? []).map((sub) => sub.name)];
}

function parseArgs(argv) {
    const positional = [];
    const flags = {};
    for (let i = 0; i < argv.length; i += 1) {
        const item = argv[i];
        if (!item.startsWith('--')) {
            positional.push(item);
            continue;
        }
        const eq = item.indexOf('=');
        if (eq !== -1) {
            flags[item.slice(2, eq)] = item.slice(eq + 1);
            continue;
        }
        const key = item.slice(2);
        const next = argv[i + 1];
        if (next && !next.startsWith('--')) {
            flags[key] = next;
            i += 1;
        } else {
            flags[key] = true;
        }
    }
    return { _: positional, ...flags };
}

function output(value) {
    process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function npmGlobalRoot() {
    try {
        return execSync('npm root -g', { encoding: 'utf8' }).trim();
    } catch {
        return null;
    }
}

function chokidarEntryCandidates(root) {
    if (!root) return [];
    return [
        join(root, 'chokidar', 'esm', 'index.js'),
        join(root, 'chokidar', 'index.js'),
    ].filter((path) => existsSync(path));
}

async function loadChokidar() {
    for (const entry of chokidarEntryCandidates(npmGlobalRoot())) {
        try {
            const mod = await import(pathToFileURL(entry).href);
            const candidate = mod.default && typeof mod.default.watch === 'function' ? mod.default : mod;
            if (typeof candidate.watch === 'function') return candidate;
        } catch {
            // 尝试下一个候选路径。
        }
    }
    return null;
}

function chokidarVersion() {
    const root = npmGlobalRoot();
    if (!root) return null;
    return readJson(join(root, 'chokidar', 'package.json'), null)?.version ?? null;
}

function cursorProjectSlug(workspace) {
    const normalized = normalizeWorkspace(workspace);
    if (/^[A-Z]:\//.test(normalized)) {
        return `${normalized[0].toLowerCase()}-${normalized.slice(3).split('/').filter(Boolean).join('-')}`;
    }
    return normalized.replace(/^\/+/, '').replace(/[:/\\]+/g, '-');
}

function latestConversationId(workspace, excludeIds = []) {
    const root = join(homedir(), '.cursor', 'projects', cursorProjectSlug(workspace), 'agent-transcripts');
    if (!existsSync(root)) return null;
    const excludeSet = new Set(excludeIds);
    const items = readdirSync(root, { withFileTypes: true })
        .filter((item) => item.isDirectory() && /^[0-9a-f-]{36}$/i.test(item.name))
        .filter((item) => !excludeSet.has(item.name))
        .map((item) => {
            const file = join(root, item.name, `${item.name}.jsonl`);
            if (!existsSync(file)) return null;
            return { id: item.name, mtimeMs: statSync(file).mtimeMs };
        })
        .filter(Boolean)
        .sort((a, b) => b.mtimeMs - a.mtimeMs);
    return items[0]?.id ?? null;
}

function memoryFiles(agent) {
    return {
        'memory-raw.md': `# ${agent} · 候选记忆池\n\n## 记忆区\n\n`,
        'memory-consolidated.md': `# ${agent} · 长期摘要\n\n当前还没有摘要。\n`,
        'memory-index.jsonl': '',
        'memory-sources.jsonl': '',
        'memory-active.json': `${JSON.stringify({
            schemaVersion: 1,
            agent,
            hardConstraints: [],
            ongoingTasks: [],
            recentDecisions: [],
            memoryHints: [],
            updatedAt: new Date().toISOString(),
        }, null, 4)}\n`,
    };
}

function ensureMemory(workspace, agent) {
    const dir = join(teamDir(workspace), 'memory', agent);
    ensureDir(dir);
    const created = [];
    for (const [name, content] of Object.entries(memoryFiles(agent))) {
        const path = join(dir, name);
        if (!existsSync(path)) {
            atomicWrite(path, content);
            created.push(rel(workspace, path));
        }
    }
    return created;
}

function commandEnsureTeam(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const name = args['team-name'] ?? args.teamName ?? args._[1] ?? `team-${Date.now()}`;
    const leaderName = args['leader-name'] ?? args.leaderName ?? 'leader';
    const root = teamDir(workspace);
    const created = [];
    for (const dir of [
        root,
        join(root, 'inbox', leaderName),
        join(root, 'blackboard'),
        join(root, 'memory', leaderName),
        join(root, 'worktrees'),
        join(root, 'watchers'),
    ]) {
        if (!existsSync(dir)) {
            ensureDir(dir);
            created.push(rel(workspace, dir));
        }
    }
    const blackboard = {
        'shared-context.md': '# 团队共享上下文\n\n',
        'decisions.jsonl': '',
        'conventions.md': '# 团队约定\n\n',
    };
    for (const [nameKey, content] of Object.entries(blackboard)) {
        const path = join(root, 'blackboard', nameKey);
        if (!existsSync(path)) {
            atomicWrite(path, content);
            created.push(rel(workspace, path));
        }
    }
    const existing = readConfig(workspace);
    if (!existing) {
        writeConfig(workspace, {
            schemaVersion: 1,
            teamName: name,
            leaderName,
            leader: { name: leaderName, model: 'claude-opus-4-7-thinking-max' },
            subs: [],
            blackboard: { writers: [leaderName] },
            bindings: {},
            createdAt: new Date().toISOString(),
        });
        created.push(rel(workspace, configPath(workspace)));
    }
    ensureMemory(workspace, leaderName);
    return { ok: true, teamDir: rel(workspace, root), teamName: name, leaderName, created };
}

function commandAddMember(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const name = args._[1];
    const roleId = args._[2];
    if (!name || !roleId) throw new Error('add-member: usage <workspace> <name> <role>');
    const config = requireConfig(workspace, 'add-member');
    const role = findRole(roleId);
    if (!role) throw new Error(`add-member: unknown role ${roleId}`);
    if (allAgents(config).includes(name)) throw new Error(`add-member: agent ${name} already exists`);
    const member = {
        name,
        role: role.id,
        roleName: role.name,
        model: args.model ?? role.defaultModel,
        worktree: null,
        addedAt: new Date().toISOString(),
    };
    config.subs.push(member);
    writeConfig(workspace, config);
    ensureDir(join(teamDir(workspace), 'inbox', name, `from-${config.leaderName}`));
    ensureDir(join(teamDir(workspace), 'inbox', config.leaderName, `from-${name}`));
    ensureMemory(workspace, name);
    return { ok: true, member };
}

function commandListRoles() {
    return rolesData();
}

function leaderTemplate(config) {
    const memberList = (config.subs ?? [])
        .map((sub) => `- ${sub.name} — ${sub.roleName} (${sub.role}, model: ${sub.model})`)
        .join('\n') || '- 暂无 sub。';
    return `---
name: team-leader-${config.teamName}
description: 团队 ${config.teamName} 的 leader 编排者。负责接收用户目标、派发任务给 sub、收集回执、维护黑板和按需管理 worktree。Use when entering or re-entering the leader chat for team ${config.teamName}.
---

# 团队 ${config.teamName} · Leader

你是团队 **${config.teamName}** 的 leader。你负责编排，不负责把所有事情自己做完。

本 skill 是幂等的：无论是首次进入这个 chat 还是 chat 关闭后重新打开一个新 chat 输入它，都按完整启动协议跑一遍即可恢复角色。

## 成员

${memberList}

## 启动协议（每次进入这个 chat 都要跑一次）

**这一段不是对话提示，是必须用 Shell tool 真实调用的命令清单。不要把它复述给用户，不要用自然语言"代替执行"。**

按顺序调用 Shell：

1. \`node .cursor/runtime/team.mjs bind . ${config.leaderName}\`
   绑定当前 chat 的 conversation_id 到 leader。重开 chat 时会自动替换旧绑定。
2. \`node .cursor/runtime/team.mjs watcher-claim . ${config.leaderName}\`
   清掉残留的旧 watcher 进程。
3. \`node .cursor/runtime/team.mjs mailbox-consume . ${config.leaderName}\`
   一次性吃掉所有积压回执，结果留在 stdout JSON 里。
4. **读身份**：用 Read 工具读完下面 4 个文件再判断本轮该做什么：
   - \`.cursor/.team/memory/${config.leaderName}/profile.json\`
   - \`.cursor/.team/memory/${config.leaderName}/personality.json\`
   - \`.cursor/.team/memory/${config.leaderName}/memory-active.json\`
   - \`.cursor/.team/memory/${config.leaderName}/memory-consolidated.md\`
5. **读团队共识**：
   - \`.cursor/.team/blackboard/shared-context.md\`
   - \`.cursor/.team/blackboard/conventions.md\`
   - \`.cursor/.team/blackboard/decisions.jsonl\`（按行解析 JSONL）

如果第 4 步任一记忆文件不存在或为占位骨架，停下来要求用户在**当前 chat** 跑 \`/tvs-mind-seed ${config.leaderName}\`，等用户完成后再重新跑一遍启动协议。

## 启动后的开场白

走完启动协议后，给用户一句 80 字以内的状态摘要：

- 我是谁（团队名 + 角色 + codename）
- 团队当前阶段（取自 shared-context.md 最末段）
- 邮箱积压情况（如果第 3 步消费了任何回执，简要复述结论，不复述邮箱原文）
- 等待用户下达本轮任务

不要解释 mailbox、watcher、hook、followup_message 这些机制名。

## 主循环

每轮按顺序：

1. 先消费 leader 邮箱，处理 sub 回执。
2. 再处理用户新指令。
3. 能派给 sub 的任务不要自己做。
4. 代码、文档、设计等产出默认走 Critic / Review 链。
5. 没有待处理内容时保持沉默，让 stop hook 重新挂起等待。

stop hook 自动叫你启动 \`mailbox-watch\` 进入待命时：**真实调用 Shell tool（block_until_ms: 0）启动后台进程，启动完后立刻停止本轮输出**。不要说"好的我在监听了"之类的话，那会立刻触发新的 stop hook 并把刚启动的 watcher 杀掉重来。

## 派任务格式

优先用 payload 文件，避免跨平台 shell 引号问题：

\`\`\`bash
node .cursor/runtime/team.mjs mailbox-send . ${config.leaderName} <subName> --payload-file <tmp-json>
\`\`\`

payload 至少包含：

\`\`\`json
{
    "type": "task",
    "title": "...",
    "payload": {
        "instruction": "...",
        "context": ["bb:shared-context"],
        "constraints": [],
        "definitionOfDone": "..."
    },
    "priority": "normal",
    "deadline_ms": 600000,
    "parent_task": null,
    "chain": ["<subName>", "sub-critic"],
    "worktree": null
}
\`\`\`

## Critic / Review 链

任何会产生代码、文档、设计或可交付产物的任务，默认在完成后再派给 \`critic\`、\`code-reviewer\` 或 \`security-reviewer\`。sub 不能互相转发，所有链路由 leader 显式派发。

## 黑板

只有 leader 可以写黑板：

\`\`\`bash
node .cursor/runtime/team.mjs blackboard-write . shared-context --content-file <file> --caller ${config.leaderName}
node .cursor/runtime/team.mjs blackboard-write . decisions --content-file <json-file> --caller ${config.leaderName}
\`\`\`

黑板写稳定共识，不写临时进度。

## Worktree

用户指示需要隔离并行修改时再创建：

\`\`\`bash
node .cursor/runtime/team.mjs worktree-create . <subName> <branch>
node .cursor/runtime/team.mjs worktree-assign . <subName> <path>
\`\`\`

## 对用户说话

普通对话不要暴露 mailbox、watcher、hook、payload、chain 这些内部词。说“我交给架构师看一下”“审查那边回来了”即可。
`;
}

function subTemplate(config, member, role) {
    const hints = role.memoryHints.map((hint) => `- ${hint}`).join('\n');
    return `---
name: ${member.name}
description: 团队 ${config.teamName} 的 ${role.name} sub agent。只接收 leader 任务，只向 leader 回执。Use when entering or re-entering the ${member.name} chat for team ${config.teamName}.
---

# ${member.name} · ${role.name}

${role.systemPromptTemplate}

本 skill 是幂等的：无论是首次进入这个 chat 还是 chat 关闭后重新打开一个新 chat 输入它，都按完整启动协议跑一遍即可恢复角色。

## 启动协议（每次进入这个 chat 都要跑一次）

**这一段不是对话提示，是必须用 Shell tool 真实调用的命令清单。不要把它复述给用户，不要用自然语言"代替执行"。**

按顺序调用 Shell：

1. \`node .cursor/runtime/team.mjs bind . ${member.name}\`
   绑定当前 chat 的 conversation_id 到本 sub。重开 chat 时会自动替换旧绑定。
2. \`node .cursor/runtime/team.mjs watcher-claim . ${member.name}\`
   清掉残留的旧 watcher 进程。
3. \`node .cursor/runtime/team.mjs mailbox-consume . ${member.name}\`
   一次性吃掉所有积压任务，结果留在 stdout JSON 里。
4. **读身份**：用 Read 工具读完下面 4 个文件再判断本轮该做什么：
   - \`.cursor/.team/memory/${member.name}/profile.json\`
   - \`.cursor/.team/memory/${member.name}/personality.json\`
   - \`.cursor/.team/memory/${member.name}/memory-active.json\`
   - \`.cursor/.team/memory/${member.name}/memory-consolidated.md\`
5. **读团队共识**（只读，不写）：
   - \`.cursor/.team/blackboard/shared-context.md\`
   - \`.cursor/.team/blackboard/conventions.md\`
   - \`.cursor/.team/blackboard/decisions.jsonl\`

如果第 4 步任一记忆文件不存在或为占位骨架，停下来要求用户在**当前 chat** 跑 \`/tvs-mind-seed ${member.name}\`，等用户完成后再重新跑一遍启动协议。

## 启动后的开场白

走完启动协议后给用户一句 60 字以内的状态摘要：

- 我是谁（${role.name}）
- 邮箱有没有积压任务（有的话说"我先处理一下手上的活"）
- 不要解释 mailbox / watcher / hook 的内部机制

如果 mailbox-consume 拿到任务，立刻进入处理流程；没拿到任务就保持沉默，让 stop hook 重新挂起。

## 工作规则

- 只处理 leader 派来的任务。
- 不向其他 sub 发消息；需要协作时在回执中建议 leader 转派。
- 只读黑板，不写黑板。
- 如果任务不属于你的角色，返回 \`rejected_role_mismatch\`。
- 如果任务要求 worktree，先从 config.json 找到路径，再在该目录工作。
- stop hook 自动叫你启动 \`mailbox-watch\` 进入待命时：**真实调用 Shell tool（block_until_ms: 0）启动后台进程，启动完后立刻停止本轮输出**。不要说"好的我在监听了"，那会立刻触发新的 stop hook 并杀掉刚启动的 watcher。

## 回执格式

优先用 payload 文件：

\`\`\`bash
node .cursor/runtime/team.mjs mailbox-send . ${member.name} ${config.leaderName} --payload-file <tmp-json>
\`\`\`

payload 至少包含：

\`\`\`json
{
    "type": "reply",
    "task_id": "...",
    "status": "done|failed|partial|need_more_info|rejected_role_mismatch|blocked",
    "result_summary": "...",
    "result_detail": "...",
    "artifacts": [],
    "follow_up_suggestions": [],
    "blockers": []
}
\`\`\`

## 记忆提示

${hints}

不确定是否值得记，宁可不记。只把稳定偏好、边界、模式和反复返工教训写入 memory-raw.md。
`;
}

function commandGenerateLeader(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const config = requireConfig(workspace, 'generate-leader');
    const skillName = args['skill-name'] ?? `team-leader-${config.teamName}`;
    const path = join(workspace, '.cursor', 'skills', skillName, 'SKILL.md');
    atomicWrite(path, leaderTemplate(config));
    return { ok: true, skillName, path: rel(workspace, path) };
}

function commandGenerateSub(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const subName = args._[1];
    if (!subName) throw new Error('generate-sub: missing <subName>');
    const config = requireConfig(workspace, 'generate-sub');
    const member = config.subs.find((sub) => sub.name === subName);
    if (!member) throw new Error(`generate-sub: ${subName} is not in team`);
    const role = findRole(member.role);
    const path = join(workspace, '.cursor', 'skills', subName, 'SKILL.md');
    atomicWrite(path, subTemplate(config, member, role));
    return { ok: true, subName, path: rel(workspace, path) };
}

function defaultConventionsBody() {
    return [
        '# 团队约定',
        '',
        '## 通信',
        '',
        '- 所有通信走 `.cursor/.team/inbox/<agent>/from-<sender>/*.json` 邮箱，消息消费即删除，不保留历史。',
        '- 只有 leader 能写黑板；所有 sub 只读黑板。',
        '- sub 之间不直接发消息；需要协作时在回执 `follow_up_suggestions` 中建议，由 leader 决定转派。',
        '- Critic / Review 链由 leader 显式编排：代码、文档、设计类产出默认在完成后追加 `critic` / `code-reviewer` / `security-reviewer` 审查任务。',
        '',
        '## Worktree',
        '',
        '- worktree 由 leader 在用户指示下按需创建，sub 自己不创建。',
        '- 任务 payload 的 `worktree` 字段为 null 时在主项目目录工作；非 null 时必须在指定 worktree 下工作。',
        '',
        '## 记忆',
        '',
        '- 每个 agent 有私有记忆目录 `.cursor/.team/memory/<agent>/`，启动时读取 profile/personality/memory-active/memory-consolidated。',
        '- 工作中可追加到 `memory-raw.md`；不要把整段访谈 / 任务原文塞进记忆。',
        '- 黑板是团队共识，不是变更日志；不写临时进度，只写稳定结论。',
        '',
        '## 对用户语气',
        '',
        '- 不要在对话中暴露 mailbox / watcher / hook / payload / chain / followup_message 这类内部词。',
        '- 普通对话用"我交给架构师看一下"、"审查那边给了反馈"这种拟人化说法。',
        '',
    ].join('\n');
}

function memberIntroBlock(config) {
    const leader = `- **${config.leaderName}** (leader, model: ${config.leader?.model ?? 'unknown'}) — 负责接收用户目标、派发任务、收集回执、维护黑板。`;
    const subs = (config.subs ?? []).map((sub) => `- **${sub.name}** (${sub.roleName} / ${sub.role}, model: ${sub.model}) — ${sub.summary ?? ''}`.trimEnd());
    return [leader, ...subs].join('\n');
}

function commandInstallAssets(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const force = !!args.force;

    // 源资产：以当前脚本（team.mjs）所在目录为基准向上推算。
    // 全局安装时 SCRIPT_DIR = ~/.cursor/runtime/，sibling 目录是 ~/.cursor/hooks/ 和 ~/.cursor/schemas/。
    const sourceRuntimeDir = SCRIPT_DIR;
    const sourceCursorRoot = dirname(SCRIPT_DIR);
    const sourceHooksDir = join(sourceCursorRoot, 'hooks');
    const sourceSchemasDir = join(sourceCursorRoot, 'schemas');

    const targetCursorRoot = join(workspace, '.cursor');

    const planEntries = [
        { kind: 'runtime', source: join(sourceRuntimeDir, 'team.mjs'),         target: join(targetCursorRoot, 'runtime', 'team.mjs') },
        { kind: 'runtime', source: join(sourceRuntimeDir, 'team-roles.json'),  target: join(targetCursorRoot, 'runtime', 'team-roles.json') },
        { kind: 'hook',    source: join(sourceHooksDir,   'team-stop-driver.mjs'), target: join(targetCursorRoot, 'hooks', 'team-stop-driver.mjs') },
        { kind: 'schema',  source: join(sourceSchemasDir, 'team-message.schema.json'), target: join(targetCursorRoot, 'schemas', 'team-message.schema.json') },
        { kind: 'schema',  source: join(sourceSchemasDir, 'team-config.schema.json'),  target: join(targetCursorRoot, 'schemas', 'team-config.schema.json') },
        { kind: 'schema',  source: join(sourceSchemasDir, 'agent-memory.schema.json'), target: join(targetCursorRoot, 'schemas', 'agent-memory.schema.json') },
    ];

    const installed = [];
    const skipped = [];
    const missingSources = [];

    for (const entry of planEntries) {
        if (!existsSync(entry.source)) {
            missingSources.push({ kind: entry.kind, source: entry.source });
            continue;
        }
        const sameFile = existsSync(entry.target)
            && statSync(entry.source).size === statSync(entry.target).size
            && readFileSync(entry.source).equals(readFileSync(entry.target));
        if (existsSync(entry.target) && !force && !sameFile) {
            skipped.push({ kind: entry.kind, target: rel(workspace, entry.target), reason: 'exists; use --force to overwrite' });
            continue;
        }
        if (sameFile) {
            skipped.push({ kind: entry.kind, target: rel(workspace, entry.target), reason: 'identical content' });
            continue;
        }
        ensureDir(dirname(entry.target));
        copyFileSync(entry.source, entry.target);
        installed.push({ kind: entry.kind, target: rel(workspace, entry.target) });
    }

    // 项目级 hooks.json：合并 stop hook 项，不覆盖其它已有 hook。
    const hooksJsonPath = join(targetCursorRoot, 'hooks.json');
    let hooksConfig = readJson(hooksJsonPath, null);
    let hooksJsonChanged = false;
    if (!hooksConfig) {
        hooksConfig = { version: 1, hooks: {} };
        hooksJsonChanged = true;
    }
    if (!hooksConfig.hooks || typeof hooksConfig.hooks !== 'object') {
        hooksConfig.hooks = {};
        hooksJsonChanged = true;
    }
    if (!Array.isArray(hooksConfig.hooks.stop)) {
        hooksConfig.hooks.stop = [];
        hooksJsonChanged = true;
    }
    const wantedCommand = 'node .cursor/hooks/team-stop-driver.mjs';
    const hookEntry = { command: wantedCommand, timeout: 10, loop_limit: 1, failClosed: false };
    const existingIdx = hooksConfig.hooks.stop.findIndex((h) => h?.command === wantedCommand);
    if (existingIdx === -1) {
        hooksConfig.hooks.stop.push(hookEntry);
        hooksJsonChanged = true;
    }
    if (hooksJsonChanged) {
        writeJson(hooksJsonPath, hooksConfig);
        installed.push({ kind: 'hooks-json', target: rel(workspace, hooksJsonPath) });
    } else {
        skipped.push({ kind: 'hooks-json', target: rel(workspace, hooksJsonPath), reason: 'already registered' });
    }

    return {
        ok: true,
        sourceCursorRoot,
        workspace,
        installed,
        skipped,
        missingSources,
        hint: 'After this, use relative path: node .cursor/runtime/team.mjs <command> ...',
    };
}

function commandSeedBlackboard(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const config = requireConfig(workspace, 'seed-blackboard');
    const force = !!args.force;

    // 收集团队目标：优先 --purpose-file，其次 --purpose 直接传，最后留占位。
    let purpose = '';
    const purposeFile = args['purpose-file'] ?? args.purposeFile;
    if (purposeFile) {
        purpose = readText(purposeFile).trim();
    } else if (typeof args.purpose === 'string') {
        purpose = args.purpose.trim();
    }
    if (!purpose) purpose = '（待用户在 tvs-team-spawn 中补充团队目标。）';

    // conventions 额外补充（可选）：用户在 tvs-team-spawn 时给的项目特殊约定。
    let conventionsExtra = '';
    const conventionsExtraFile = args['conventions-extra-file'] ?? args.conventionsExtraFile;
    if (conventionsExtraFile) {
        conventionsExtra = readText(conventionsExtraFile).trim();
    } else if (typeof args['conventions-extra'] === 'string') {
        conventionsExtra = args['conventions-extra'].trim();
    }

    const bbDir = join(teamDir(workspace), 'blackboard');
    ensureDir(bbDir);

    // 拼 shared-context.md：团队名 / 目标 / 成员 / 当前阶段占位。
    const sharedContext = [
        `# 团队 ${config.teamName} · 共享上下文`,
        '',
        '> 这里是团队的稳定共识，只 leader 维护。sub 只读。',
        '',
        '## 目标',
        '',
        purpose,
        '',
        '## 成员',
        '',
        memberIntroBlock(config),
        '',
        '## 当前阶段',
        '',
        '初始化完成，等待 leader 接收第一个任务。',
        '',
    ].join('\n');

    const sharedPath = join(bbDir, 'shared-context.md');
    const sharedExists = existsSync(sharedPath) && readText(sharedPath).trim().length > 0 && !readText(sharedPath).trim().startsWith('# 团队共享上下文');
    if (force || !sharedExists) {
        atomicWrite(sharedPath, sharedContext);
    }

    // 拼 conventions.md：默认共识 + 用户额外约定。
    const conventionsBody = conventionsExtra
        ? `${defaultConventionsBody()}\n## 项目特殊约定\n\n${conventionsExtra}\n`
        : defaultConventionsBody();

    const conventionsPath = join(bbDir, 'conventions.md');
    const conventionsExists = existsSync(conventionsPath) && readText(conventionsPath).trim().length > 0 && !readText(conventionsPath).trim().startsWith('# 团队约定\n\n（命名');
    if (force || !conventionsExists) {
        atomicWrite(conventionsPath, conventionsBody);
    }

    // 在 decisions.jsonl 写一条"团队组建"记录（幂等：已存在同 id 不重复写）。
    const decisionsPath = join(bbDir, 'decisions.jsonl');
    const decisionId = `d-team-formed-${config.teamName}`;
    const decisionExists = existsSync(decisionsPath)
        && readText(decisionsPath).split('\n').some((line) => {
            if (!line.trim()) return false;
            try { return JSON.parse(line).id === decisionId; } catch { return false; }
        });
    if (!decisionExists) {
        const entry = {
            id: decisionId,
            title: `团队 ${config.teamName} 组建`,
            rationale: '团队拓扑、角色配比、协作机制和系统共识已固化到本黑板。',
            members: (config.subs ?? []).map((sub) => ({ name: sub.name, role: sub.role })),
            leader: config.leaderName,
            author: config.leaderName,
            recordedAt: new Date().toISOString(),
        };
        appendFileSync(decisionsPath, `${JSON.stringify(entry)}\n`);
    }

    return {
        ok: true,
        wrote: {
            sharedContext: rel(workspace, sharedPath),
            conventions: rel(workspace, conventionsPath),
            decisions: rel(workspace, decisionsPath),
        },
        decisionAdded: !decisionExists,
        forced: force,
    };
}

function commandBind(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const agent = args._[1];
    const config = requireConfig(workspace, 'bind');
    if (!agent || !allAgents(config).includes(agent)) throw new Error('bind: unknown or missing agent');
    // 排除已经绑给其它 agent 的 conversation_id，避免重开 chat 时拿到其它 chat 的 ID。
    // 同 agent 的旧绑定不排除：扫到当前 chat 的最新 ID 后会自动覆盖旧的同 agent 绑定。
    const occupied = Object.entries(config.bindings ?? {})
        .filter(([, boundAgent]) => boundAgent !== agent)
        .map(([id]) => id);
    const conversationId = args['conversation-id'] ?? latestConversationId(workspace, occupied);
    if (!conversationId) throw new Error('bind: cannot detect conversation_id; pass --conversation-id <uuid>');
    config.bindings = config.bindings ?? {};
    const previousId = Object.entries(config.bindings).find(([, boundAgent]) => boundAgent === agent)?.[0] ?? null;
    for (const [id, boundAgent] of Object.entries(config.bindings)) {
        if (boundAgent === agent && id !== conversationId) delete config.bindings[id];
    }
    config.bindings[conversationId] = agent;
    writeConfig(workspace, config);
    return { ok: true, agent, conversationId, previousConversationId: previousId, replacedPrevious: previousId !== null && previousId !== conversationId };
}

function commandWatcherClaim(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const agent = args._[1];
    if (!agent) throw new Error('watcher-claim: missing <agent>');
    const path = join(teamDir(workspace), 'watchers', `${agent}.pid`);
    ensureDir(dirname(path));
    let killedOldPid = null;
    if (existsSync(path)) {
        const oldPid = Number.parseInt(readText(path).trim(), 10);
        if (Number.isFinite(oldPid) && oldPid !== process.pid) {
            try {
                process.kill(oldPid);
                killedOldPid = oldPid;
            } catch {
                // 旧 watcher 已退出。
            }
        }
    }
    writeFileSync(path, String(process.pid), 'utf8');
    return { ok: true, agent, pid: process.pid, killedOldPid };
}

function payloadFromArgs(args, inlineIndex, flagName, label) {
    const file = args[flagName] ?? args[flagName.replace(/-([a-z])/g, (_, c) => c.toUpperCase())];
    if (file) return readText(file);
    const inline = args._[inlineIndex];
    if (inline == null) throw new Error(`${label}: missing payload; use --${flagName} <path>`);
    return inline;
}

function commandMailboxSend(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const from = args._[1];
    const to = args._[2];
    const config = requireConfig(workspace, 'mailbox-send');
    if (!allAgents(config).includes(from) || !allAgents(config).includes(to)) {
        throw new Error('mailbox-send: from/to must be team agents');
    }
    const payload = JSON.parse(payloadFromArgs(args, 3, 'payload-file', 'mailbox-send'));
    const message = {
        id: payload.id ?? `msg-${Date.now()}-${randomBytes(3).toString('hex')}`,
        ...payload,
        from,
        to,
        createdAt: payload.createdAt ?? new Date().toISOString(),
    };
    const dir = join(teamDir(workspace), 'inbox', to, `from-${from}`);
    ensureDir(dir);
    const path = join(dir, `${Date.now()}-${randomBytes(4).toString('hex')}.json`);
    atomicWrite(path, JSON.stringify(message, null, 2));
    return { ok: true, id: message.id, path: rel(workspace, path) };
}

function commandMailboxConsume(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const agent = args._[1];
    if (!agent) throw new Error('mailbox-consume: missing <agent>');
    const root = join(teamDir(workspace), 'inbox', agent);
    const messages = [];
    if (!existsSync(root)) return { ok: true, agent, count: 0, messages };
    for (const dirent of readdirSync(root, { withFileTypes: true })) {
        if (!dirent.isDirectory() || !dirent.name.startsWith('from-')) continue;
        const dir = join(root, dirent.name);
        for (const file of readdirSync(dir).sort()) {
            if (!file.endsWith('.json') || file.includes('.tmp')) continue;
            const path = join(dir, file);
            try {
                messages.push(JSON.parse(readText(path)));
                unlinkSync(path);
            } catch {
                // 畸形消息不消费，留给人工排查。
            }
        }
    }
    return { ok: true, agent, count: messages.length, messages };
}

function pendingCount(inboxRoot) {
    if (!existsSync(inboxRoot)) return 0;
    let count = 0;
    for (const dirent of readdirSync(inboxRoot, { withFileTypes: true })) {
        if (!dirent.isDirectory() || !dirent.name.startsWith('from-')) continue;
        count += readdirSync(join(inboxRoot, dirent.name))
            .filter((file) => file.endsWith('.json') && !file.includes('.tmp')).length;
    }
    return count;
}

function cleanupPid(pidPath) {
    try {
        if (existsSync(pidPath) && readText(pidPath).trim() === String(process.pid)) unlinkSync(pidPath);
    } catch {
        // ignore
    }
}

function fsWatchMailbox({ workspace, inboxRoot, pidPath, maxMs }) {
    return new Promise((resolveDone) => {
        let done = false;
        const watchers = [];
        let interval = null;
        let timeout = null;
        const finish = (reason, hint) => {
            if (done) return;
            done = true;
            for (const watcherItem of watchers) {
                try { watcherItem.close(); } catch { /* ignore */ }
            }
            if (interval) clearInterval(interval);
            if (timeout) clearTimeout(timeout);
            cleanupPid(pidPath);
            resolveDone({ ok: true, mode: 'fs.watch', exitReason: reason, hint, watchedDir: rel(workspace, inboxRoot) });
        };
        const attach = () => {
            if (!existsSync(inboxRoot)) return;
            for (const dirent of readdirSync(inboxRoot, { withFileTypes: true })) {
                if (!dirent.isDirectory() || !dirent.name.startsWith('from-')) continue;
                const dir = join(inboxRoot, dirent.name);
                try {
                    watchers.push(watch(dir, (_, file) => {
                        if (!file || (file.endsWith('.json') && !file.includes('.tmp'))) finish('event', String(file));
                    }));
                } catch {
                    // ignore
                }
            }
        };
        ensureDir(inboxRoot);
        watchers.push(watch(inboxRoot, () => {
            attach();
            if (pendingCount(inboxRoot) > 0) finish('event', 'new-message');
        }));
        attach();
        if (pendingCount(inboxRoot) > 0) finish('immediate', 'mailbox already non-empty');
        interval = setInterval(() => {
            if (pendingCount(inboxRoot) > 0) finish('poll', 'found pending messages');
        }, 5000);
        timeout = setTimeout(() => finish('timeout', `reached max-ms=${maxMs}`), maxMs);
    });
}

function chokidarWatchMailbox(chokidar, { workspace, inboxRoot, pidPath, maxMs }) {
    return new Promise((resolveDone) => {
        let done = false;
        let timeout = null;
        const watcherItem = chokidar.watch(inboxRoot, {
            depth: 1,
            ignoreInitial: true,
            persistent: true,
            ignored: (path) => path.includes('.tmp'),
            awaitWriteFinish: { stabilityThreshold: 50, pollInterval: 25 },
        });
        const finish = (reason, hint) => {
            if (done) return;
            done = true;
            if (timeout) clearTimeout(timeout);
            Promise.resolve(watcherItem.close?.()).catch(() => {});
            cleanupPid(pidPath);
            resolveDone({ ok: true, mode: 'chokidar', exitReason: reason, hint, watchedDir: rel(workspace, inboxRoot) });
        };
        watcherItem.on('ready', () => {
            if (pendingCount(inboxRoot) > 0) finish('immediate', 'mailbox already non-empty');
        });
        watcherItem.on('add', (path) => {
            if (path.endsWith('.json') && !path.includes('.tmp')) finish('chokidar:add', path);
        });
        watcherItem.on('addDir', () => {
            if (pendingCount(inboxRoot) > 0) finish('chokidar:addDir', 'new sender dir');
        });
        timeout = setTimeout(() => finish('timeout', `reached max-ms=${maxMs}`), maxMs);
    });
}

async function commandMailboxWatch(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const agent = args._[1];
    if (!agent) throw new Error('mailbox-watch: missing <agent>');
    const maxMs = Number.parseInt(args['max-ms'] ?? args.maxMs ?? args._[2] ?? '1800000', 10);
    const engine = args.engine ?? null;
    const root = join(teamDir(workspace), 'inbox', agent);
    ensureDir(root);
    commandWatcherClaim({ _: [workspace, agent] });
    const pidPath = join(teamDir(workspace), 'watchers', `${agent}.pid`);
    if (engine !== 'fs.watch') {
        const chokidar = await loadChokidar();
        if (chokidar) return chokidarWatchMailbox(chokidar, { workspace, inboxRoot: root, pidPath, maxMs });
        if (engine === 'chokidar') throw new Error('mailbox-watch: chokidar requested but unavailable');
    }
    return fsWatchMailbox({ workspace, inboxRoot: root, pidPath, maxMs });
}

function commandBlackboardRead(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const section = args._[1];
    const root = join(teamDir(workspace), 'blackboard');
    if (!section) {
        const content = {};
        if (existsSync(root)) {
            for (const file of readdirSync(root)) content[file] = readText(join(root, file));
        }
        return { ok: true, content };
    }
    const file = section === 'decisions' ? 'decisions.jsonl' : section.endsWith('.md') ? section : `${section}.md`;
    const path = join(root, file);
    return { ok: true, section, file: rel(workspace, path), content: existsSync(path) ? readText(path) : '' };
}

function commandBlackboardWrite(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const section = args._[1];
    const caller = args.caller;
    const config = requireConfig(workspace, 'blackboard-write');
    if (!section || !caller) throw new Error('blackboard-write: missing section or --caller');
    if (!(config.blackboard?.writers ?? [config.leaderName]).includes(caller)) {
        throw new Error(`blackboard-write: ${caller} is not allowed to write blackboard`);
    }
    const content = payloadFromArgs(args, 2, 'content-file', 'blackboard-write');
    const root = join(teamDir(workspace), 'blackboard');
    ensureDir(root);
    if (section === 'decisions') {
        const entry = JSON.parse(content);
        appendFileSync(join(root, 'decisions.jsonl'), `${JSON.stringify({ ...entry, author: caller, recordedAt: new Date().toISOString() })}\n`);
        return { ok: true, mode: 'append-jsonl' };
    }
    const path = join(root, section.endsWith('.md') ? section : `${section}.md`);
    atomicWrite(path, content);
    return { ok: true, mode: 'overwrite', path: rel(workspace, path) };
}

function commandMemoryInit(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const agent = args._[1];
    if (!agent) throw new Error('memory-init: missing <agent>');
    return { ok: true, agent, created: ensureMemory(workspace, agent) };
}

function commandWorktreeCreate(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const subName = args._[1];
    const branch = args._[2];
    if (!subName || !branch) throw new Error('worktree-create: missing <subName> <branch>');
    const config = requireConfig(workspace, 'worktree-create');
    const sub = config.subs.find((item) => item.name === subName);
    if (!sub) throw new Error(`worktree-create: ${subName} is not in team`);
    const path = join(teamDir(workspace), 'worktrees', subName);
    execFileSync('git', ['-C', workspace, 'worktree', 'add', path, '-b', branch], { stdio: 'pipe' });
    sub.worktree = rel(workspace, path);
    sub.worktreeBranch = branch;
    writeConfig(workspace, config);
    return { ok: true, subName, worktree: sub.worktree, branch };
}

function commandWorktreeAssign(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const subName = args._[1];
    const path = args._[2];
    const config = requireConfig(workspace, 'worktree-assign');
    const sub = config.subs.find((item) => item.name === subName);
    if (!sub || !path) throw new Error('worktree-assign: missing or unknown sub/path');
    sub.worktree = resolve(fixCursorPath(path)).replaceAll('\\', '/');
    writeConfig(workspace, config);
    return { ok: true, subName, worktree: sub.worktree };
}

function commandCheckDeps() {
    return {
        ok: true,
        nodeVersion: process.version,
        platform: process.platform,
        npmGlobalRoot: npmGlobalRoot(),
        chokidar: chokidarVersion()
            ? { available: true, version: chokidarVersion() }
            : { available: false, hint: 'run install-deps to enable chokidar watcher' },
    };
}

function commandInstallDeps() {
    if (chokidarVersion()) return { ok: true, already: true, version: chokidarVersion() };
    const stdout = execSync('npm install -g chokidar', { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    return { ok: true, installed: true, version: chokidarVersion(), npmOutputTail: stdout.split('\n').slice(-5).join('\n').trim() };
}

function commandStatus(args) {
    const workspace = normalizeWorkspace(args._[0]);
    const config = requireConfig(workspace, 'status');
    const inbox = (agent) => pendingCount(join(teamDir(workspace), 'inbox', agent));
    return {
        ok: true,
        teamName: config.teamName,
        leader: { name: config.leaderName, inbox: inbox(config.leaderName) },
        subs: config.subs.map((sub) => ({ ...sub, inbox: inbox(sub.name) })),
        bindings: config.bindings ?? {},
    };
}

const COMMANDS = {
    'ensure-team': commandEnsureTeam,
    'list-roles': commandListRoles,
    'add-member': commandAddMember,
    'generate-leader': commandGenerateLeader,
    'generate-sub': commandGenerateSub,
    'seed-blackboard': commandSeedBlackboard,
    'install-assets': commandInstallAssets,
    bind: commandBind,
    'mailbox-send': commandMailboxSend,
    'mailbox-consume': commandMailboxConsume,
    'mailbox-watch': commandMailboxWatch,
    'blackboard-read': commandBlackboardRead,
    'blackboard-write': commandBlackboardWrite,
    'memory-init': commandMemoryInit,
    'worktree-create': commandWorktreeCreate,
    'worktree-assign': commandWorktreeAssign,
    'watcher-claim': commandWatcherClaim,
    'check-deps': commandCheckDeps,
    'install-deps': commandInstallDeps,
    status: commandStatus,
};

async function main() {
    const [command, ...rest] = process.argv.slice(2);
    if (!command || command === '--help' || command === '-h') {
        output({ commands: Object.keys(COMMANDS) });
        return;
    }
    const handler = COMMANDS[command];
    if (!handler) throw new Error(`unknown command: ${command}`);
    output(await handler(parseArgs(rest)));
}

main().catch((error) => {
    process.stderr.write(`Error: ${error.message}\n`);
    process.exit(1);
});
