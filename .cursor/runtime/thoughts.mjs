#!/usr/bin/env node
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

/**
 * 全局 fallback ROOT,只有没有任何项目本地 .cursor/.thoughts/ 命中时才使用。
 */
const GLOBAL_ROOT = join(homedir(), '.cursor', '.thoughts');

let ROOT = GLOBAL_ROOT;
let ACTIVE_FILE = join(ROOT, 'active.json');
let SESSIONS_FILE = join(ROOT, 'sessions.json');

/**
 * 解析当前应该使用的 thoughts ROOT。优先级:
 *   1. 环境变量 THOUGHTS_ROOT
 *   2. 从 hint(或 cwd)向上查找,命中 `<dir>/.cursor/.thoughts/` 即用(且不同于全局)
 *   3. 全局 ~/.cursor/.thoughts/
 */
function resolveRoot(hint) {
    if (process.env.THOUGHTS_ROOT) {
        return resolve(fixCursorPath(process.env.THOUGHTS_ROOT));
    }

    const start = hint ? resolve(fixCursorPath(hint)) : process.cwd();
    const globalAbs = resolve(GLOBAL_ROOT);

    let dir = start;
    while (true) {
        const candidate = join(dir, '.cursor', '.thoughts');
        if (existsSync(candidate) && resolve(candidate) !== globalAbs) {
            return candidate;
        }
        const parent = dirname(dir);
        if (parent === dir) break;
        dir = parent;
    }

    return GLOBAL_ROOT;
}

/**
 * 在每个命令入口调用,根据命令上下文(workspace 参数或 cwd)切换 ROOT。
 */
function initRoot(hint) {
    ROOT = resolveRoot(hint);
    ACTIVE_FILE = join(ROOT, 'active.json');
    SESSIONS_FILE = join(ROOT, 'sessions.json');
}

function ensureDir(path) {
    mkdirSync(path, { recursive: true });
}

function readJson(path, fallback) {
    try {
        return JSON.parse(readFileSync(path, 'utf8'));
    } catch {
        return fallback;
    }
}

function writeJson(path, value) {
    ensureDir(dirname(path));
    writeFileSync(path, `${JSON.stringify(value, null, 4)}\n`, 'utf8');
}

function appendJsonl(path, value) {
    ensureDir(dirname(path));
    writeFileSync(path, `${JSON.stringify(value)}\n`, { flag: 'a' });
}

/**
 * Cursor 在 Windows 下会用 unix 风格的盘符路径,例如 `/e:/inksnow/Thoughts`。
 * 需要先剥掉前导斜杠,否则 `resolve` 会返回 `E:\e:\...` 这种垃圾。
 */
function fixCursorPath(value) {
    if (typeof value !== 'string') return value;
    if (process.platform === 'win32' && /^\/[a-z]:/i.test(value)) {
        return value.substring(1);
    }
    return value;
}

function normalizeWorkspace(value) {
    let result = resolve(fixCursorPath(value) || process.cwd()).replaceAll('\\', '/');
    if (process.platform === 'win32' && /^[a-z]:/.test(result)) {
        result = result[0].toUpperCase() + result.slice(1);
    }
    return result;
}

function activeState() {
    return readJson(ACTIVE_FILE, {});
}

function saveActive(state) {
    writeJson(ACTIVE_FILE, state);
}

function instanceDir(name) {
    return join(ROOT, 'instances', name);
}

function latestSession(workspace) {
    const sessions = readJson(SESSIONS_FILE, {});
    return sessions[workspace] ?? null;
}

function listInstances() {
    const root = join(ROOT, 'instances');
    ensureDir(root);
    const items = readdirSync(root, { withFileTypes: true })
        .filter((item) => item.isDirectory())
        .map((item) => item.name)
        .sort((a, b) => a.localeCompare(b));
    console.log(JSON.stringify(items, null, 4));
}

function bind(instanceName, workspaceArg) {
    const workspace = normalizeWorkspace(workspaceArg);
    const session = latestSession(workspace);

    if (!session?.conversation_id) {
        throw new Error(`没有找到 workspace ${workspace} 的最新 Cursor session。请在一个新 chat 中运行 /thoughts。`);
    }
    if (session.conversation_id === 'conv-test') {
        throw new Error('检测到 dry-run 测试 session(conv-test),拒绝绑定。请重载 Cursor 后在真实专用 chat 中重新运行 /thoughts。');
    }
    const sessionAgeMs = Date.now() - Date.parse(session.updated_at ?? 0);
    if (!Number.isFinite(sessionAgeMs) || sessionAgeMs > 60 * 60 * 1000) {
        throw new Error(`最新 Cursor session 记录过旧或无效(${session.updated_at ?? 'unknown'}),请重载 Cursor 后在真实专用 chat 中重新运行 /thoughts。`);
    }

    const state = activeState();
    const dir = instanceDir(instanceName);
    ensureDir(dir);

    const now = Date.now();
    state[workspace] = {
        instance: instanceName,
        conversation_id: session.conversation_id,
        transcript_path: session.transcript_path ?? null,
        enabled: true,
        next_active_at: now,
        updated_at: new Date(now).toISOString(),
    };
    saveActive(state);
    console.log(JSON.stringify(state[workspace], null, 4));
}

function unbind(workspaceArg) {
    const workspace = normalizeWorkspace(workspaceArg);
    const state = activeState();
    const removed = state[workspace] ?? null;
    delete state[workspace];
    saveActive(state);
    console.log(JSON.stringify({ removed }, null, 4));
}

function schedule(workspaceArg, delayMsArg, reason = 'scheduled') {
    const workspace = normalizeWorkspace(workspaceArg);
    const delayMs = Math.max(0, Number(delayMsArg || 0));
    const state = activeState();
    const entry = state[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const nextActiveAt = Date.now() + delayMs;
    entry.next_active_at = nextActiveAt;
    entry.next_active_at_iso = new Date(nextActiveAt).toISOString();
    entry.last_schedule_reason = reason;
    delete entry.timer_active_until;
    delete entry.timer_active_until_iso;
    delete entry.timer_started_at;
    delete entry.timer_started_at_iso;
    entry.updated_at = new Date().toISOString();
    state[workspace] = entry;
    saveActive(state);
    console.log(JSON.stringify(entry, null, 4));
}

function loopStatePath(instanceName) {
    return join(instanceDir(instanceName), 'loop-state.json');
}

function activityLogPath(instanceName) {
    return join(instanceDir(instanceName), 'activity-log.jsonl');
}

const THOUGHT_SOURCE_RANKING = [
    'longThread',
    'personaMood',
    'worldObservation',
    'tasteReaction',
    'associativeDrift',
];

const SOURCE_SCORE_BONUS = {
    longThread: 0.35,
    personaMood: 0.25,
    worldObservation: 0.2,
    tasteReaction: 0.12,
    associativeDrift: 0.08,
};

const INTERNAL_USER_FACING_TERMS = [
    '闹钟',
    '候选队列',
    'candidateQueue',
    '潜意识',
    'record-active',
    'timer',
    'active state',
    'mind-state',
    'subagent',
    'hook',
    '锚点',
];

const RECENT_CHAT_REFERENCE_RE = /用户刚|上一轮|你刚才说|刚才那|最近一轮|上轮/i;

function defaultPermissions() {
    return {
        version: 1,
        updatedAt: new Date().toISOString(),
        signals: {
            time: 'always',
            workspace: 'always',
            gitStatus: 'always',
            devServers: 'always',
            systemStatus: 'ask',
            activeApp: 'ask',
            windowTitle: 'ask',
            weather: 'ask',
            browserTabs: 'deny',
            clipboard: 'deny',
            calendar: 'deny',
            recentFiles: 'deny',
            terminalLogs: 'ask',
        },
        pendingRequests: [],
    };
}

function defaultMindState() {
    const now = new Date().toISOString();

    return {
        schemaVersion: 1,
        updatedAt: now,
        personaState: {
            mood: 'calm-curious',
            energy: 0.55,
            socialBattery: 0.65,
            toneBias: ['自然短句', '轻微吐槽', '少程序感'],
            currentAttitude: '少做信息搬运,多给可复述的判断。',
        },
        editorialPolicy: {
            coreStance: [
                '信息不稀缺,可复述的判断稀缺。',
                '主动内容要推进一个思考线程,不是随机抽卡。',
                '少围着用户当前工作进度转,多讲结构、隐喻和判断。',
            ],
            messageShape: {
                mustHaveJudgment: true,
                preferContinuation: true,
                avoidPureFactDump: true,
                includeAftertaste: true,
                hideInternalMechanics: true,
                forbiddenUserFacingTerms: [
                    '闹钟',
                    '候选队列',
                    'candidateQueue',
                    '潜意识',
                    'record-active',
                    'timer',
                    'active state',
                    'mind-state',
                    'subagent',
                    'hook',
                ],
            },
        },
        threads: [
            {
                id: 'class_mobility',
                title: '阶层流动与低成本试错',
                stance: '普通人真正稀缺的是失败后还能继续行动的空间。',
                openQuestions: ['怎么给自己造第一个存档点?'],
                energy: 0.75,
                cooldownRounds: 0,
                lastTouchedAt: null,
            },
            {
                id: 'identity_in_ai_age',
                title: 'AI 时代的人格副本权',
                stance: '平台未来不只是审核内容,也要审核身份授权。',
                openQuestions: ['数字分身被滥用时,平台责任边界在哪里?'],
                energy: 0.7,
                cooldownRounds: 0,
                lastTouchedAt: null,
            },
            {
                id: 'post_writing',
                title: '写帖子的方法论',
                stance: '帖子最稀缺的是一句能被复述的判断。',
                openQuestions: ['怎么把新闻压缩成观点,再展开成故事?'],
                energy: 0.65,
                cooldownRounds: 0,
                lastTouchedAt: null,
            },
            {
                id: 'architecture_complexity',
                title: '架构复杂度与变化成本',
                stance: '架构设计要先找最痛的变化,再决定边界。',
                openQuestions: ['当前系统里哪个变化最贵?'],
                energy: 0.65,
                cooldownRounds: 0,
                lastTouchedAt: null,
            },
        ],
        candidateQueue: [],
        selectionPolicy: {
            ownThoughtSourceRanking: THOUGHT_SOURCE_RANKING,
            sourceScoreBonus: SOURCE_SCORE_BONUS,
            recentTopicBuckets: [],
            avoidSameBucketRounds: 2,
            maxSameBucketInRecentSix: 2,
            modeWeights: {
                threadContinuation: 0.45,
                newDiscovery: 0.2,
                counterpoint: 0.15,
                casual: 0.15,
                quiet: 0.05,
            },
            shortTermDownrank: [],
        },
        subconscious: {
            lastPreparedAt: null,
            lastRunReason: null,
            targetQueueSize: 5,
            minQueueSize: 2,
        },
    };
}

function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
}

function isQuietHour(now, quietHours = []) {
    if (!Array.isArray(quietHours) || quietHours.length !== 2) return false;

    const [start, end] = quietHours.map(Number);
    const hour = new Date(now).getHours();

    if (start === end) return false;
    if (start < end) return hour >= start && hour < end;
    return hour >= start || hour < end;
}

function hasEngagementBoostSignal(preview = '') {
    const text = String(preview ?? '').trim();
    if (!text) return false;
    return /(继续|展开|多说|多讲|想听|感兴趣|喜欢|不错|这个好|讲讲|细说|可以继续|就这个)/i.test(text);
}

function recordUser(workspaceArg, preview = '') {
    const workspace = normalizeWorkspace(workspaceArg);
    const state = activeState();
    const entry = state[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const now = Date.now();
    const path = loopStatePath(entry.instance);
    const loopState = readJson(path, {});

    loopState.last_user_message_at = now;
    loopState.last_user_message_at_iso = new Date(now).toISOString();
    loopState.last_user_message_preview = String(preview ?? '').slice(0, 240);
    loopState.consecutive_ignored = 0;

    writeJson(path, loopState);
    appendJsonl(activityLogPath(entry.instance), {
        time: new Date(now).toISOString(),
        action: 'user_message',
        preview: loopState.last_user_message_preview,
    });

    console.log(JSON.stringify(loopState, null, 4));
}

function normalizeModeAndTopic(modeArg = 'active', topicArg = 'active_message') {
    const allowedModes = new Set(['discovery', 'ambient', 'casual', 'reflection', 'quiet']);
    if (allowedModes.has(modeArg)) {
        return {
            mode: modeArg,
            topic: topicArg || modeArg,
        };
    }

    // Backward compatible: old callers passed only topic.
    return {
        mode: 'active',
        topic: [modeArg, topicArg].filter(Boolean).join(' ') || 'active_message',
    };
}

function pushRecentMode(loopState, mode) {
    const recent = Array.isArray(loopState.recent_behavior_modes)
        ? loopState.recent_behavior_modes
        : [];
    recent.push({
        mode,
        time: new Date().toISOString(),
    });
    loopState.recent_behavior_modes = recent.slice(-10);
    loopState.last_behavior_mode = mode;
}

function recordActive(workspaceArg, modeArg = 'active', topicArg = 'active_message') {
    const workspace = normalizeWorkspace(workspaceArg);
    const state = activeState();
    const entry = state[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const { mode, topic } = normalizeModeAndTopic(modeArg, topicArg);
    const isQuiet = mode === 'quiet';
    const now = Date.now();
    const dir = instanceDir(entry.instance);
    const personality = readJson(join(dir, 'personality.json'), {});
    const rhythm = personality.rhythm ?? {};
    const minDelayMs = Number(rhythm.minDelayMs ?? 15 * 60 * 1000);
    const baseDelayMs = Number(rhythm.baseDelayMs ?? 30 * 60 * 1000);
    const maxDelayMs = Number(rhythm.maxDelayMs ?? 2 * 60 * 60 * 1000);
    const decayMultiplier = Number(rhythm.decayMultiplier ?? 1.5);
    const boostMultiplier = Number(rhythm.boostMultiplier ?? 0.7);

    const path = loopStatePath(entry.instance);
    const loopState = readJson(path, {});
    const previousActiveAt = Number(loopState.last_active_message_at || 0);
    const lastUserAt = Number(loopState.last_user_message_at || 0);
    const previousActiveWasIgnored = !isQuiet && previousActiveAt > 0 && lastUserAt < previousActiveAt;
    const consecutiveIgnored = isQuiet
        ? Number(loopState.consecutive_ignored || 0)
        : previousActiveWasIgnored
            ? Number(loopState.consecutive_ignored || 0) + 1
            : 0;

    let delayMs;
    let delayReason;

    if (isQuiet) {
        delayMs = clamp(baseDelayMs, minDelayMs, maxDelayMs);
        delayReason = 'quiet mode';
    } else if (isQuietHour(now, rhythm.quietHours)) {
        delayMs = maxDelayMs;
        delayReason = 'quiet hours';
    } else if (consecutiveIgnored >= 3) {
        delayMs = maxDelayMs;
        delayReason = 'three or more consecutive ignored messages';
    } else if (consecutiveIgnored > 0) {
        delayMs = clamp(Math.round(baseDelayMs * (decayMultiplier ** consecutiveIgnored)), minDelayMs, maxDelayMs);
        delayReason = `${consecutiveIgnored} consecutive ignored message(s)`;
    } else if (lastUserAt > previousActiveAt
        && now - lastUserAt <= 20 * 60 * 1000
        && hasEngagementBoostSignal(loopState.last_user_message_preview)) {
        delayMs = clamp(Math.round(baseDelayMs * boostMultiplier), minDelayMs, maxDelayMs);
        delayReason = 'explicit user engagement';
    } else {
        delayMs = clamp(baseDelayMs, minDelayMs, maxDelayMs);
        delayReason = 'baseline rhythm';
    }

    loopState.consecutive_ignored = consecutiveIgnored;
    pushRecentMode(loopState, mode);
    if (isQuiet) {
        loopState.last_quiet_at = now;
        loopState.last_quiet_at_iso = new Date(now).toISOString();
        loopState.last_quiet_topic = topic;
    } else {
        loopState.last_active_message_at = now;
        loopState.last_active_message_at_iso = new Date(now).toISOString();
        loopState.last_active_topic = topic;
    }
    loopState.last_delay_ms = delayMs;
    loopState.last_delay_reason = delayReason;
    writeJson(path, loopState);

    appendJsonl(activityLogPath(entry.instance), {
        time: new Date(now).toISOString(),
        action: isQuiet ? 'quiet' : 'active_message',
        mode,
        topic,
        delayMs,
        delayReason,
        consecutiveIgnored,
    });

    const nextActiveAt = now + delayMs;
    entry.next_active_at = nextActiveAt;
    entry.next_active_at_iso = new Date(nextActiveAt).toISOString();
    entry.last_schedule_reason = `dynamic: ${delayReason}`;
    delete entry.timer_active_until;
    delete entry.timer_active_until_iso;
    delete entry.timer_started_at;
    delete entry.timer_started_at_iso;
    entry.updated_at = new Date(now).toISOString();
    state[workspace] = entry;
    saveActive(state);

    console.log(JSON.stringify({
        mode,
        topic,
        delayMs,
        delayReason,
        consecutiveIgnored,
        next_active_at: nextActiveAt,
        next_active_at_iso: entry.next_active_at_iso,
    }, null, 4));
}

function normalizeThoughtSource(value, candidateType = '') {
    if (THOUGHT_SOURCE_RANKING.includes(value)) return value;
    switch (candidateType) {
        case 'threadContinuation':
            return 'longThread';
        case 'discovery':
        case 'ambient':
        case 'newDiscovery':
            return 'worldObservation';
        case 'counterpoint':
            return 'tasteReaction';
        case 'casual':
            return 'associativeDrift';
        default:
            return 'longThread';
    }
}

function normalizeSourceRanking(selection = {}) {
    const ranking = Array.isArray(selection.ownThoughtSourceRanking)
        ? selection.ownThoughtSourceRanking.filter((source) => THOUGHT_SOURCE_RANKING.includes(source))
        : [];
    for (const source of THOUGHT_SOURCE_RANKING) {
        if (!ranking.includes(source)) ranking.push(source);
    }
    return ranking;
}

function sourceBonusMap(selection = {}) {
    const configured = selection.sourceScoreBonus;
    if (configured && typeof configured === 'object' && !Array.isArray(configured)) {
        return { ...SOURCE_SCORE_BONUS, ...configured };
    }
    return SOURCE_SCORE_BONUS;
}

function modeForCandidate(candidate) {
    if (candidate.type === 'quiet') return 'quiet';
    if (candidate.type === 'discovery' || candidate.type === 'newDiscovery') return 'discovery';
    if (candidate.type === 'ambient') return 'ambient';
    if (candidate.type === 'casual') return 'casual';
    return 'reflection';
}

function readMindState(instanceName) {
    const path = join(instanceDir(instanceName), 'mind-state.json');
    return {
        path,
        state: readJson(path, defaultMindState()),
    };
}

function isExpired(candidate, now) {
    if (!candidate.expiresAt) return false;
    const expiresAt = Date.parse(candidate.expiresAt);
    return Number.isFinite(expiresAt) && expiresAt < now;
}

function activeDownrankBuckets(selection = {}) {
    const buckets = new Set();
    for (const entry of selection.shortTermDownrank ?? []) {
        if (entry?.topicBucket) buckets.add(entry.topicBucket);
    }
    return buckets;
}

function scoreCandidate(candidate, mindState, now = Date.now()) {
    const selection = mindState.selectionPolicy ?? {};
    const ranking = normalizeSourceRanking(selection);
    const bonuses = sourceBonusMap(selection);
    const thoughtSource = normalizeThoughtSource(candidate.thoughtSource, candidate.type);
    const sourceRank = ranking.indexOf(thoughtSource);
    const recentBuckets = Array.isArray(selection.recentTopicBuckets)
        ? selection.recentTopicBuckets
        : [];
    const recentSix = recentBuckets.slice(-6);
    const maxSame = Number(selection.maxSameTopicBucketInRecentSix ?? 2);
    const thread = (mindState.threads ?? []).find((item) => item.id === candidate.threadId);
    const downrankBuckets = activeDownrankBuckets(selection);
    const reasons = [];
    let penalty = 0;

    if (isExpired(candidate, now)) {
        return {
            eligible: false,
            adjustedScore: -Infinity,
            thoughtSource,
            sourceRank,
            reasons: ['expired'],
        };
    }

    if (downrankBuckets.has(candidate.topicBucket)) {
        penalty += 0.2;
        reasons.push(`downranked bucket: ${candidate.topicBucket}`);
    }

    if (recentSix.at(-1) === candidate.topicBucket) {
        penalty += 0.18;
        reasons.push(`same as previous bucket: ${candidate.topicBucket}`);
    }

    const recentCount = recentSix.filter((bucket) => bucket === candidate.topicBucket).length;
    if (recentCount >= maxSame) {
        penalty += 0.28;
        reasons.push(`bucket saturated in recent six: ${candidate.topicBucket}`);
    }

    if (Number(thread?.cooldownRounds ?? 0) > 0) {
        const cooldownPenalty = Math.min(0.25, Number(thread.cooldownRounds) * 0.08);
        penalty += cooldownPenalty;
        reasons.push(`thread cooldown: ${thread.cooldownRounds}`);
    }

    const moodText = [
        mindState.personaState?.mood,
        mindState.personaState?.currentAttitude,
        ...(mindState.personaState?.toneBias ?? []),
    ].join(' ');
    const moodHint = [candidate.mood, ...(candidate.expressionHints ?? [])].join(' ');
    if (moodText && moodHint && moodText.includes(candidate.mood)) {
        reasons.push('mood aligned');
    }

    const rawScore = Number(candidate.score ?? 0);
    const sourceBonus = Number(bonuses[thoughtSource] ?? 0);
    const rankTieBreaker = sourceRank >= 0 ? (ranking.length - sourceRank) / 1000 : 0;
    const adjustedScore = rawScore + sourceBonus + rankTieBreaker - penalty;

    return {
        eligible: true,
        adjustedScore,
        rawScore,
        thoughtSource,
        sourceRank,
        sourceBonus,
        penalty,
        reasons,
    };
}

function selectThought(workspaceArg, options = {}) {
    const workspace = normalizeWorkspace(workspaceArg);
    const entry = activeState()[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const { state: mindState, path } = readMindState(entry.instance);
    const selection = mindState.selectionPolicy ?? {};
    const now = Date.now();
    const minScore = Number(selection.minEligibleScore ?? 0.7);
    const scored = (mindState.candidateQueue ?? [])
        .map((candidate) => ({
            candidate,
            score: scoreCandidate(candidate, mindState, now),
        }))
        .filter(({ score }) => score.eligible)
        .sort((a, b) => b.score.adjustedScore - a.score.adjustedScore);

    const chosen = scored[0];
    const result = {
        ok: true,
        workspace,
        instance: entry.instance,
        mindStatePath: path,
        sourceRanking: normalizeSourceRanking(selection),
        recentTopicBuckets: selection.recentTopicBuckets ?? [],
        mode: 'quiet',
        shouldSpeak: false,
        quietReason: 'no eligible candidate',
        candidatesConsidered: scored.slice(0, 5).map(({ candidate, score }) => ({
            id: candidate.id,
            type: candidate.type,
            thoughtSource: score.thoughtSource,
            topicBucket: candidate.topicBucket,
            rawScore: score.rawScore,
            adjustedScore: Number(score.adjustedScore.toFixed(3)),
            reasons: score.reasons,
        })),
    };

    if (!chosen || chosen.score.adjustedScore < minScore) {
        if (chosen) result.quietReason = `top candidate below threshold ${minScore}`;
        if (!options.returnOnly) console.log(JSON.stringify(result, null, 4));
        return result;
    }

    const { candidate, score } = chosen;
    const mode = modeForCandidate(candidate);
    const decision = {
        candidateId: candidate.id,
        threadId: candidate.threadId,
        mode,
        topic: candidate.topicBucket || candidate.threadId || score.thoughtSource,
        thoughtSource: score.thoughtSource,
        sourceRank: score.sourceRank,
        topicBucket: candidate.topicBucket,
        mood: candidate.mood,
        stance: candidate.stance,
        aftertaste: candidate.aftertaste ?? '',
        observation: candidate.observation,
        expressionHints: candidate.expressionHints ?? [],
        rawScore: score.rawScore,
        adjustedScore: Number(score.adjustedScore.toFixed(3)),
        reasons: score.reasons,
    };

    Object.assign(result, {
        mode,
        shouldSpeak: mode !== 'quiet',
        quietReason: mode === 'quiet' ? 'selected quiet candidate' : null,
        decision,
    });

    if (!options.returnOnly) console.log(JSON.stringify(result, null, 4));
    return result;
}

function consumeThought(workspaceArg, candidateId, modeArg = 'active', topicArg = '') {
    if (!candidateId) throw new Error('consume-thought requires a candidateId');
    const workspace = normalizeWorkspace(workspaceArg);
    const entry = activeState()[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const { state: mindState, path } = readMindState(entry.instance);
    const queue = Array.isArray(mindState.candidateQueue) ? mindState.candidateQueue : [];
    const candidate = queue.find((item) => item.id === candidateId);
    if (!candidate) {
        throw new Error(`candidate ${candidateId} not found`);
    }

    const now = new Date().toISOString();
    mindState.candidateQueue = queue.filter((item) => item.id !== candidateId);
    mindState.selectionPolicy = mindState.selectionPolicy ?? {};
    const recent = Array.isArray(mindState.selectionPolicy.recentTopicBuckets)
        ? mindState.selectionPolicy.recentTopicBuckets
        : [];
    const bucket = candidate.topicBucket || topicArg || candidate.threadId || 'active';
    mindState.selectionPolicy.recentTopicBuckets = [...recent, bucket].slice(-12);

    const avoidRounds = Number(mindState.selectionPolicy.avoidSameBucketRounds ?? 2);
    for (const thread of mindState.threads ?? []) {
        if (thread.id === candidate.threadId) {
            thread.lastTouchedAt = now;
            thread.cooldownRounds = Math.max(Number(thread.cooldownRounds ?? 0), avoidRounds);
        } else if (Number(thread.cooldownRounds ?? 0) > 0) {
            thread.cooldownRounds = Math.max(0, Number(thread.cooldownRounds) - 1);
        }
    }

    mindState.updatedAt = now;
    writeJson(path, mindState);

    appendJsonl(activityLogPath(entry.instance), {
        time: now,
        action: 'consume_candidate',
        candidateId,
        mode: modeArg,
        topic: topicArg || bucket,
        thoughtSource: normalizeThoughtSource(candidate.thoughtSource, candidate.type),
        topicBucket: bucket,
    });

    console.log(JSON.stringify({
        ok: true,
        instance: entry.instance,
        consumed: candidateId,
        topicBucket: bucket,
        remainingCandidates: mindState.candidateQueue.length,
    }, null, 4));
}

function dryRunActive(workspaceArg) {
    const decision = selectThought(workspaceArg, { returnOnly: true });
    console.log(JSON.stringify({
        ok: true,
        dryRun: true,
        decision,
    }, null, 4));
}

function validateThoughtState(valueArg) {
    const instanceName = resolveInstanceName(valueArg);
    if (!instanceName) {
        throw new Error('需要实例名或已绑定的 workspace 才能校验 thought state。');
    }

    const { state: mindState, path } = readMindState(instanceName);
    const errors = [];
    const warnings = [];
    const now = Date.now();
    const queue = Array.isArray(mindState.candidateQueue) ? mindState.candidateQueue : [];
    const recentReferenceCount = queue.filter((candidate) => RECENT_CHAT_REFERENCE_RE.test([
        candidate.observation,
        candidate.stance,
        candidate.messageDraft,
    ].join(' '))).length;

    const selection = mindState.selectionPolicy ?? {};
    const ranking = normalizeSourceRanking(selection);
    if (ranking.join('|') !== THOUGHT_SOURCE_RANKING.join('|')) {
        warnings.push(`source ranking differs from default own-thought order: ${ranking.join(' > ')}`);
    }

    for (const [index, candidate] of queue.entries()) {
        const label = `candidateQueue[${index}](${candidate.id ?? '<missing id>'})`;
        if (!candidate.thoughtSource) errors.push(`${label} missing thoughtSource`);
        if (candidate.thoughtSource && !THOUGHT_SOURCE_RANKING.includes(candidate.thoughtSource)) {
            errors.push(`${label} has invalid thoughtSource ${candidate.thoughtSource}`);
        }
        if (isExpired(candidate, now)) warnings.push(`${label} is expired`);
        const text = [candidate.observation, candidate.stance, candidate.aftertaste, candidate.messageDraft].join(' ');
        for (const term of INTERNAL_USER_FACING_TERMS) {
            if (text.includes(term)) warnings.push(`${label} contains internal term "${term}"`);
        }
        if (RECENT_CHAT_REFERENCE_RE.test(text)) warnings.push(`${label} references recent chat in candidate body`);
    }

    const recentReferenceRatio = queue.length === 0 ? 0 : recentReferenceCount / queue.length;
    if (recentReferenceRatio > 0.2) {
        warnings.push(`recent-chat candidate ratio ${recentReferenceRatio.toFixed(2)} exceeds 0.20`);
    }

    const result = {
        ok: errors.length === 0,
        instance: instanceName,
        path,
        candidateCount: queue.length,
        recentReferenceRatio,
        errors,
        warnings,
    };
    console.log(JSON.stringify(result, null, 4));
    if (errors.length > 0) process.exitCode = 1;
}

function showState(workspaceArg) {
    const workspace = normalizeWorkspace(workspaceArg);
    console.log(JSON.stringify(activeState()[workspace] ?? null, null, 4));
}

function resolveInstanceName(value) {
    if (!value || value === '.') {
        const workspace = normalizeWorkspace(value);
        const entry = activeState()[workspace];
        if (entry?.instance) return entry.instance;
    }

    if (value) {
        const workspace = normalizeWorkspace(value);
        const entry = activeState()[workspace];
        if (entry?.instance) return entry.instance;
    }

    return value;
}

function validateArray(value, path, errors, options = {}) {
    if (!Array.isArray(value)) {
        errors.push(`${path} must be an array`);
        return;
    }
    if (options.minItems !== undefined && value.length < options.minItems) {
        errors.push(`${path} must contain at least ${options.minItems} item(s)`);
    }
}

function validateNumber(value, path, errors, min = 0, max = 1) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
        errors.push(`${path} must be a number`);
        return;
    }
    if (value < min || value > max) {
        errors.push(`${path} must be between ${min} and ${max}`);
    }
}

function validateMindState(valueArg) {
    const instanceName = resolveInstanceName(valueArg);
    if (!instanceName) {
        throw new Error('需要实例名或已绑定的 workspace 才能校验 mind-state。');
    }

    const path = join(instanceDir(instanceName), 'mind-state.json');
    const state = readJson(path, null);
    const errors = [];

    if (!state || typeof state !== 'object') {
        errors.push('mind-state.json must be a JSON object');
    } else {
        if (state.schemaVersion !== 1) errors.push('schemaVersion must be 1');
        if (typeof state.updatedAt !== 'string') errors.push('updatedAt must be a string');

        const persona = state.personaState;
        if (!persona || typeof persona !== 'object') {
            errors.push('personaState must be an object');
        } else {
            if (typeof persona.mood !== 'string') errors.push('personaState.mood must be a string');
            validateNumber(persona.energy, 'personaState.energy', errors);
            validateNumber(persona.socialBattery, 'personaState.socialBattery', errors);
            validateArray(persona.toneBias, 'personaState.toneBias', errors);
            if (typeof persona.currentAttitude !== 'string') errors.push('personaState.currentAttitude must be a string');
        }

        const editorial = state.editorialPolicy;
        if (!editorial || typeof editorial !== 'object') {
            errors.push('editorialPolicy must be an object');
        } else {
            validateArray(editorial.coreStance, 'editorialPolicy.coreStance', errors, { minItems: 1 });
            const shape = editorial.messageShape;
            if (!shape || typeof shape !== 'object') {
                errors.push('editorialPolicy.messageShape must be an object');
            } else {
                for (const key of ['mustHaveJudgment', 'preferContinuation', 'avoidPureFactDump', 'includeAftertaste', 'hideInternalMechanics']) {
                    if (typeof shape[key] !== 'boolean') errors.push(`editorialPolicy.messageShape.${key} must be a boolean`);
                }
                validateArray(shape.forbiddenUserFacingTerms, 'editorialPolicy.messageShape.forbiddenUserFacingTerms', errors, { minItems: 1 });
                const requiredTerms = ['闹钟', '候选队列', 'candidateQueue', '潜意识', 'record-active', 'timer', 'active state', 'mind-state', 'subagent', 'hook'];
                for (const term of requiredTerms) {
                    if (!shape.forbiddenUserFacingTerms?.includes(term)) {
                        errors.push(`forbiddenUserFacingTerms must include ${term}`);
                    }
                }
            }
        }

        validateArray(state.threads, 'threads', errors);
        for (const [index, thread] of (state.threads ?? []).entries()) {
            if (!thread || typeof thread !== 'object') {
                errors.push(`threads[${index}] must be an object`);
                continue;
            }
            for (const key of ['id', 'title', 'stance']) {
                if (typeof thread[key] !== 'string') errors.push(`threads[${index}].${key} must be a string`);
            }
            validateArray(thread.openQuestions, `threads[${index}].openQuestions`, errors);
            validateNumber(thread.energy, `threads[${index}].energy`, errors);
            if (typeof thread.cooldownRounds !== 'number') errors.push(`threads[${index}].cooldownRounds must be a number`);
        }

        validateArray(state.candidateQueue, 'candidateQueue', errors);
        for (const [index, candidate] of (state.candidateQueue ?? []).entries()) {
            if (!candidate || typeof candidate !== 'object') {
                errors.push(`candidateQueue[${index}] must be an object`);
                continue;
            }
            for (const key of ['id', 'threadId', 'type', 'mood', 'observation', 'stance', 'topicBucket', 'expiresAt']) {
                if (typeof candidate[key] !== 'string') errors.push(`candidateQueue[${index}].${key} must be a string`);
            }
            if (candidate.messageDraft !== undefined && typeof candidate.messageDraft !== 'string') {
                errors.push(`candidateQueue[${index}].messageDraft must be a string when present`);
            }
            if (candidate.aftertaste !== undefined && typeof candidate.aftertaste !== 'string') {
                errors.push(`candidateQueue[${index}].aftertaste must be a string when present`);
            }
            if (candidate.thoughtSource !== undefined && !THOUGHT_SOURCE_RANKING.includes(candidate.thoughtSource)) {
                errors.push(`candidateQueue[${index}].thoughtSource must be one of ${THOUGHT_SOURCE_RANKING.join(', ')}`);
            }
            if (candidate.expressionHints !== undefined) {
                validateArray(candidate.expressionHints, `candidateQueue[${index}].expressionHints`, errors);
            }
            validateNumber(candidate.score, `candidateQueue[${index}].score`, errors);
            if (!candidate.stance || candidate.stance.length < 8) {
                errors.push(`candidateQueue[${index}].stance must contain a usable judgment`);
            }
        }

        const selection = state.selectionPolicy;
        if (!selection || typeof selection !== 'object') {
            errors.push('selectionPolicy must be an object');
        } else {
            validateArray(selection.recentTopicBuckets, 'selectionPolicy.recentTopicBuckets', errors);
            if (typeof selection.avoidSameBucketRounds !== 'number') errors.push('selectionPolicy.avoidSameBucketRounds must be a number');
            if (typeof selection.maxSameBucketInRecentSix !== 'number') errors.push('selectionPolicy.maxSameBucketInRecentSix must be a number');
            if (selection.ownThoughtSourceRanking !== undefined) {
                validateArray(selection.ownThoughtSourceRanking, 'selectionPolicy.ownThoughtSourceRanking', errors, { minItems: 1 });
                for (const source of selection.ownThoughtSourceRanking ?? []) {
                    if (!THOUGHT_SOURCE_RANKING.includes(source)) {
                        errors.push(`selectionPolicy.ownThoughtSourceRanking contains invalid source ${source}`);
                    }
                }
            }
            if (selection.sourceScoreBonus !== undefined && (typeof selection.sourceScoreBonus !== 'object' || Array.isArray(selection.sourceScoreBonus))) {
                errors.push('selectionPolicy.sourceScoreBonus must be an object when present');
            }
            if (!selection.modeWeights || typeof selection.modeWeights !== 'object') {
                errors.push('selectionPolicy.modeWeights must be an object');
            }
            validateArray(selection.shortTermDownrank, 'selectionPolicy.shortTermDownrank', errors);
        }

        const subconscious = state.subconscious;
        if (!subconscious || typeof subconscious !== 'object') {
            errors.push('subconscious must be an object');
        } else {
            if (typeof subconscious.targetQueueSize !== 'number') errors.push('subconscious.targetQueueSize must be a number');
            if (typeof subconscious.minQueueSize !== 'number') errors.push('subconscious.minQueueSize must be a number');
        }
    }

    const result = {
        ok: errors.length === 0,
        instance: instanceName,
        path,
        errors,
    };
    console.log(JSON.stringify(result, null, 4));
    if (errors.length > 0) process.exitCode = 1;
}

function ensureInstanceFiles(instanceName) {
    const dir = instanceDir(instanceName);
    ensureDir(dir);

    const defaults = {
        'memory-raw.md': '# 思绪记忆 - 原始\n\n',
        'memory-consolidated.md': '# 思绪记忆 - 整理\n\n',
        'memory-active.json': '{\n    "version": 1,\n    "updatedAt": null,\n    "items": []\n}\n',
        'memory-index.jsonl': '',
        'memory-sources.jsonl': '',
        'activity-log.jsonl': '',
        'permissions.json': `${JSON.stringify(defaultPermissions(), null, 4)}\n`,
        'mind-state.json': `${JSON.stringify(defaultMindState(), null, 4)}\n`,
    };

    for (const [file, content] of Object.entries(defaults)) {
        const path = join(dir, file);
        if (!existsSync(path)) writeFileSync(path, content, 'utf8');
    }

    const loopStatePath = join(dir, 'loop-state.json');
    if (!existsSync(loopStatePath)) {
        writeJson(loopStatePath, {
            last_subconscious_at: null,
            consecutive_ignored: 0,
            last_user_message_at: null,
            last_active_message_at: null,
        });
    }

    console.log(dir);
}

function permissionState(instanceName) {
    const path = join(instanceDir(instanceName), 'permissions.json');
    const state = readJson(path, null);
    if (state) return state;
    const fallback = defaultPermissions();
    writeJson(path, fallback);
    return fallback;
}

function setPermission(instanceName, signal, value) {
    const allowedValues = new Set(['always', 'ask', 'deny']);
    if (!allowedValues.has(value)) {
        throw new Error(`权限值必须是 always / ask / deny,收到: ${value}`);
    }

    const path = join(instanceDir(instanceName), 'permissions.json');
    const state = permissionState(instanceName);
    state.signals = state.signals ?? {};
    state.signals[signal] = value;
    state.pendingRequests = (state.pendingRequests ?? []).filter((request) => request.signal !== signal);
    state.updatedAt = new Date().toISOString();
    writeJson(path, state);
    console.log(JSON.stringify(state, null, 4));
}

function run(command, args, options = {}) {
    const result = spawnSync(command, args, {
        encoding: 'utf8',
        windowsHide: true,
        timeout: options.timeout ?? 5000,
        shell: options.shell ?? false,
        cwd: options.cwd,
    });
    return {
        ok: result.status === 0,
        stdout: String(result.stdout ?? '').trim(),
        stderr: String(result.stderr ?? '').trim(),
    };
}

function powershell(script, timeout = 5000) {
    return run('powershell', ['-NoProfile', '-Command', script], { timeout });
}

const SENSITIVE_TEXT_PATTERN = /(authorization|cookie|password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)/i;
const SENSITIVE_FILE_PATTERN = /(^\.env($|\.)|secret|token|credential|private-key|id_rsa|\.pem$|\.p12$)/i;

function truncate(value, maxLength = 500) {
    const text = String(value ?? '').replaceAll('\r\n', '\n').trim();
    if (text.length <= maxLength) return text;
    return `${text.slice(0, maxLength - 1)}…`;
}

function redactSensitiveText(value) {
    return String(value ?? '')
        .split(/\r?\n/)
        .map((line) => (SENSITIVE_TEXT_PATTERN.test(line) ? '[redacted-sensitive-line]' : line))
        .join('\n');
}

function safePreview(value, maxLength = 500) {
    return truncate(redactSensitiveText(value), maxLength);
}

function relativeWorkspacePath(workspace, path) {
    const normalized = normalizeWorkspace(path);
    const prefix = `${workspace}/`;
    const relative = normalized.startsWith(prefix) ? normalized.slice(prefix.length) : normalized;
    const name = relative.split('/').at(-1) ?? relative;
    if (SENSITIVE_FILE_PATTERN.test(name)) {
        return {
            path: '[sensitive config file]',
            sensitive: true,
        };
    }
    return {
        path: relative,
        sensitive: false,
    };
}

function cursorProjectSlug(workspace) {
    const normalized = normalizeWorkspace(workspace);
    if (/^[A-Z]:\//.test(normalized)) {
        const drive = normalized[0].toLowerCase();
        const rest = normalized.slice(3).split('/').filter(Boolean).join('-');
        return `${drive}-${rest}`;
    }
    return normalized.replace(/^\/+/, '').replace(/[:/\\]+/g, '-');
}

function collectGitSnapshot(workspace) {
    const branch = run('git', ['branch', '--show-current'], { timeout: 5000, cwd: workspace });
    const status = run('git', ['status', '--short'], { timeout: 5000, cwd: workspace });
    const unstagedSummary = run('git', ['diff', '--shortstat'], { timeout: 5000, cwd: workspace });
    const stagedSummary = run('git', ['diff', '--cached', '--shortstat'], { timeout: 5000, cwd: workspace });
    const unstagedFiles = run('git', ['diff', '--name-only'], { timeout: 5000, cwd: workspace });
    const stagedFiles = run('git', ['diff', '--cached', '--name-only'], { timeout: 5000, cwd: workspace });

    const changedFiles = new Set();
    for (const output of [unstagedFiles.stdout, stagedFiles.stdout]) {
        for (const line of output.split(/\r?\n/).filter(Boolean)) {
            changedFiles.add(relativeWorkspacePath(workspace, join(workspace, line)).path);
        }
    }
    const changedFileList = [...changedFiles].slice(0, 20);
    const fallbackStatus = changedFileList.map((file) => `modified: ${file}`).join('\n');

    return {
        branch: branch.ok ? branch.stdout || null : null,
        statusPreview: status.ok && status.stdout ? safePreview(status.stdout, 1200) : fallbackStatus || null,
        unstagedSummary: unstagedSummary.ok ? unstagedSummary.stdout || null : null,
        stagedSummary: stagedSummary.ok ? stagedSummary.stdout || null : null,
        changedFiles: changedFileList,
    };
}

function collectRecentWorkspaceFiles(workspace, limit = 10) {
    const ignoredDirectories = new Set([
        '.git',
        'node_modules',
        'dist',
        'build',
        '.next',
        '.turbo',
        '.cache',
        '.cursor/.thoughts',
    ]);
    const files = [];
    const stack = [workspace];
    let scanned = 0;

    while (stack.length > 0 && scanned < 6000) {
        const dir = stack.pop();
        let entries = [];
        try {
            entries = readdirSync(dir, { withFileTypes: true });
        } catch {
            continue;
        }

        for (const entry of entries) {
            scanned += 1;
            const fullPath = join(dir, entry.name);
            const relative = normalizeWorkspace(fullPath).slice(`${workspace}/`.length);
            if (entry.isDirectory()) {
                if ([...ignoredDirectories].some((ignored) => relative === ignored || relative.startsWith(`${ignored}/`))) {
                    continue;
                }
                stack.push(fullPath);
                continue;
            }
            if (!entry.isFile()) continue;
            try {
                const stat = statSync(fullPath);
                const safePath = relativeWorkspacePath(workspace, fullPath);
                files.push({
                    ...safePath,
                    modifiedAt: stat.mtime.toISOString(),
                });
            } catch {
                // ignore transient filesystem errors
            }
        }
    }

    return files
        .sort((a, b) => Date.parse(b.modifiedAt) - Date.parse(a.modifiedAt))
        .slice(0, limit);
}

function terminalDirectoryForWorkspace(workspace) {
    return join(homedir(), '.cursor', 'projects', cursorProjectSlug(workspace), 'terminals');
}

function parseTerminalMetadata(lines) {
    const metadata = {};
    let inHeader = false;
    for (const line of lines) {
        if (line === '---') {
            if (inHeader) break;
            inHeader = true;
            continue;
        }
        if (!inHeader) continue;
        const match = line.match(/^([^:]+):\s*(.*)$/);
        if (match) metadata[match[1]] = match[2].replace(/^"|"$/g, '');
    }
    return metadata;
}

function terminalBodyPreview(lines) {
    const body = [];
    let headerSeparators = 0;
    for (const line of lines) {
        if (line === '---') {
            headerSeparators += 1;
            continue;
        }
        if (headerSeparators < 2) continue;
        if (/^(exit_code|elapsed_ms|ended_at):/.test(line)) continue;
        if (line.trim()) body.push(line);
    }
    return safePreview(body.slice(-4).join('\n'), 600);
}

function collectTerminalLogSummaries(workspace, limit = 4) {
    const terminalDir = terminalDirectoryForWorkspace(workspace);
    if (!existsSync(terminalDir)) {
        return {
            available: false,
            terminals: [],
        };
    }

    const files = readdirSync(terminalDir)
        .filter((name) => name.endsWith('.txt'))
        .map((name) => {
            const path = join(terminalDir, name);
            try {
                return { name, path, mtimeMs: statSync(path).mtimeMs };
            } catch {
                return null;
            }
        })
        .filter(Boolean)
        .sort((a, b) => b.mtimeMs - a.mtimeMs)
        .slice(0, limit);

    return {
        available: true,
        terminals: files.map((file) => {
            const text = readFileSync(file.path, 'utf8');
            const lines = text.split(/\r?\n/);
            const metadata = parseTerminalMetadata(lines);
            const exitMatch = text.match(/exit_code:\s*([^\s]+)/);
            return {
                command: safePreview(metadata.command ?? metadata.last_command ?? '', 220),
                cwd: safePreview(metadata.cwd ?? '', 160),
                running: !exitMatch,
                exitCode: exitMatch?.[1] ?? null,
                outputPreview: terminalBodyPreview(lines),
            };
        }),
    };
}

function summarizeDevServers(raw) {
    const parsed = readJsonFromString(raw, null);
    const list = Array.isArray(parsed) ? parsed : parsed ? [parsed] : [];
    return list
        .map((item) => ({
            address: item.LocalAddress ?? item.localAddress ?? null,
            port: item.LocalPort ?? item.localPort ?? null,
            processId: item.OwningProcess ?? item.processId ?? null,
        }))
        .filter((item) => item.port)
        .slice(0, 20);
}

function collectEnvironmentSnapshot(workspace, context, allowed) {
    return {
        capturedAt: new Date().toISOString(),
        scope: 'lightweight',
        privacy: {
            readsFileContents: false,
            readsClipboard: false,
            usesScreenshots: false,
            note: 'Only metadata and short redacted previews are included.',
        },
        focus: {
            activeApp: context.activeApp ?? null,
            windowTitle: context.windowTitle ? safePreview(context.windowTitle, 180) : null,
        },
        git: allowed('gitStatus') ? collectGitSnapshot(workspace) : null,
        devServers: allowed('devServers') ? summarizeDevServers(context.devServers) : null,
        recentFiles: allowed('recentFiles') ? collectRecentWorkspaceFiles(workspace) : null,
        terminalLogs: allowed('terminalLogs') ? collectTerminalLogSummaries(workspace) : null,
        unsupportedSignals: [
            allowed('browserTabs') ? 'browserTabs collector is not implemented in runtime yet' : null,
            allowed('calendar') ? 'calendar collector is not implemented in runtime yet' : null,
        ].filter(Boolean),
    };
}

function collectContext(workspaceArg) {
    const workspace = normalizeWorkspace(workspaceArg);
    const state = activeState();
    const entry = state[workspace];
    if (!entry?.enabled) {
        throw new Error(`workspace ${workspace} 没有激活思绪模式。`);
    }

    const permissions = permissionState(entry.instance);
    const signals = permissions.signals ?? {};
    const allowed = (name) => signals[name] === 'always';
    const now = new Date();
    const context = {
        time: allowed('time') ? {
            iso: now.toISOString(),
            local: now.toLocaleString(),
            hour: now.getHours(),
            weekday: now.toLocaleDateString(undefined, { weekday: 'long' }),
        } : null,
        workspace: allowed('workspace') ? { path: workspace } : null,
        availableSignals: Object.entries(signals)
            .filter(([, value]) => value === 'always')
            .map(([key]) => key),
        deniedSignals: Object.entries(signals)
            .filter(([, value]) => value === 'deny')
            .map(([key]) => key),
        askSignals: Object.entries(signals)
            .filter(([, value]) => value === 'ask')
            .map(([key]) => key),
        pendingRequests: permissions.pendingRequests ?? [],
    };

    if (allowed('gitStatus')) {
        const status = run('git', ['status', '--short'], { timeout: 5000 });
        context.gitStatus = status.ok ? status.stdout.slice(0, 2000) : null;
    }

    if (allowed('devServers')) {
        if (platform() === 'win32') {
            const ports = powershell('Get-NetTCPConnection -State Listen | Select-Object -First 20 LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress', 5000);
            context.devServers = ports.ok ? ports.stdout.slice(0, 4000) : null;
        } else {
            const ports = run('sh', ['-lc', 'command -v lsof >/dev/null 2>&1 && lsof -iTCP -sTCP:LISTEN -P | head -20 || true'], { timeout: 5000 });
            context.devServers = ports.stdout.slice(0, 4000);
        }
    }

    if (allowed('systemStatus')) {
        if (platform() === 'win32') {
            const system = powershell('Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory,TotalVisibleMemorySize,LastBootUpTime | ConvertTo-Json -Compress', 5000);
            context.systemStatus = system.ok ? system.stdout.slice(0, 2000) : null;
        } else {
            const system = run('sh', ['-lc', 'uptime; df -h . | tail -1'], { timeout: 5000 });
            context.systemStatus = system.stdout.slice(0, 2000);
        }
    }

    if (allowed('activeApp') || allowed('windowTitle')) {
        if (platform() === 'win32') {
            const script = `
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class Win32 {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
"@
$h=[Win32]::GetForegroundWindow()
$sb=New-Object System.Text.StringBuilder 512
[void][Win32]::GetWindowText($h,$sb,$sb.Capacity)
$pid32=0
[void][Win32]::GetWindowThreadProcessId($h,[ref]$pid32)
$p=Get-Process -Id $pid32 -ErrorAction SilentlyContinue
[pscustomobject]@{ process=$p.ProcessName; title=$sb.ToString() } | ConvertTo-Json -Compress
`;
            const active = powershell(script, 5000);
            if (active.ok) {
                const parsed = readJsonFromString(active.stdout, {});
                if (allowed('activeApp')) context.activeApp = parsed.process ?? null;
                if (allowed('windowTitle')) context.windowTitle = parsed.title ?? null;
            }
        } else if (platform() === 'darwin') {
            if (allowed('activeApp')) {
                const app = run('osascript', ['-e', 'tell application "System Events" to get name of first application process whose frontmost is true'], { timeout: 5000 });
                context.activeApp = app.stdout || null;
            }
            if (allowed('windowTitle')) {
                const title = run('osascript', ['-e', 'tell application "System Events" to tell process (name of first application process whose frontmost is true) to get name of front window'], { timeout: 5000 });
                context.windowTitle = title.stdout || null;
            }
        }
    }

    context.environmentSnapshot = collectEnvironmentSnapshot(workspace, context, allowed);

    console.log(JSON.stringify(context, null, 4));
}

function readJsonFromString(value, fallback) {
    try {
        return JSON.parse(value);
    } catch {
        return fallback;
    }
}

function notify(title, subtitle, message) {
    const safeTitle = String(title ?? '思绪');
    const safeSubtitle = String(subtitle ?? '');
    const safeMessage = String(message ?? '');
    const currentPlatform = platform();

    let executable;
    let args;

    if (currentPlatform === 'win32') {
        const escapedTitle = safeTitle.replaceAll("'", "''");
        const escapedSubtitle = safeSubtitle.replaceAll("'", "''");
        const escapedMessage = safeMessage.replaceAll("'", "''");
        executable = 'powershell';
        args = ['-NoProfile', '-Command', [
            "$ErrorActionPreference = 'SilentlyContinue'",
            "if (Get-Module -ListAvailable -Name BurntToast) {",
            `  New-BurntToastNotification -Text '${escapedTitle}', '${escapedSubtitle}', '${escapedMessage}' | Out-Null`,
            '} else {',
            `  Write-Output '[thoughts notification] ${escapedTitle} ${escapedSubtitle} ${escapedMessage}'`,
            '}',
        ].join('; ')];
    } else if (currentPlatform === 'darwin') {
        const escapedTitle = safeTitle.replaceAll('"', '\\"');
        const escapedSubtitle = safeSubtitle.replaceAll('"', '\\"');
        const escapedMessage = safeMessage.replaceAll('"', '\\"');
        executable = 'osascript';
        args = ['-e', `display notification "${escapedMessage}" with title "${escapedTitle}" subtitle "${escapedSubtitle}"`];
    } else {
        executable = 'sh';
        args = ['-lc', [
            'if command -v notify-send >/dev/null 2>&1; then',
            `notify-send "${safeTitle.replaceAll('"', '\\"')}" "${`${safeSubtitle} ${safeMessage}`.replaceAll('"', '\\"')}";`,
            'else',
            `printf '%s\\n' "[thoughts notification] ${safeTitle} ${safeSubtitle} ${safeMessage}";`,
            'fi',
        ].join(' ')];
    }

    const result = spawnSync(executable, args, {
        encoding: 'utf8',
        windowsHide: true,
    });

    if (result.stdout) process.stdout.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
    process.exitCode = result.status ?? 0;
}

const [cmd, ...args] = process.argv.slice(2);

/**
 * 根据命令决定 ROOT 解析的 hint。
 * 接收 workspace 参数的命令直接用第一个参数;否则用 cwd。
 */
function commandHint(name, argv) {
    switch (name) {
        case 'bind':
            return argv[1];
        case 'unbind':
        case 'schedule':
        case 'record-user':
        case 'record-active':
        case 'context':
        case 'state':
        case 'select-thought':
        case 'consume-thought':
        case 'dry-run-active':
        case 'validate-mind-state':
        case 'validate-thought-state':
            return argv[0];
        default:
            return undefined;
    }
}

initRoot(commandHint(cmd, args));
ensureDir(ROOT);

try {
    switch (cmd) {
        case 'root':
            console.log(ROOT);
            break;
        case 'list-instances':
            listInstances();
            break;
        case 'instance-dir':
            console.log(instanceDir(args[0]));
            break;
        case 'ensure-instance':
            ensureInstanceFiles(args[0]);
            break;
        case 'bind':
            bind(args[0], args[1]);
            break;
        case 'unbind':
            unbind(args[0]);
            break;
        case 'schedule':
            schedule(args[0], args[1], args.slice(2).join(' '));
            break;
        case 'record-user':
            recordUser(args[0], args.slice(1).join(' '));
            break;
        case 'record-active':
            recordActive(args[0], args[1], args.slice(2).join(' '));
            break;
        case 'select-thought':
            selectThought(args[0]);
            break;
        case 'consume-thought':
            consumeThought(args[0], args[1], args[2], args.slice(3).join(' '));
            break;
        case 'dry-run-active':
            dryRunActive(args[0]);
            break;
        case 'context':
            collectContext(args[0]);
            break;
        case 'set-permission':
            setPermission(args[0], args[1], args[2]);
            break;
        case 'state':
            showState(args[0]);
            break;
        case 'validate-mind-state':
            validateMindState(args[0] ?? '.');
            break;
        case 'validate-thought-state':
            validateThoughtState(args[0] ?? '.');
            break;
        case 'notify':
            notify(args[0], args[1], args.slice(2).join(' '));
            break;
        default:
            console.log(`Usage:
  node .cursor/runtime/thoughts.mjs root
  node .cursor/runtime/thoughts.mjs list-instances
  node .cursor/runtime/thoughts.mjs ensure-instance <name>
  node .cursor/runtime/thoughts.mjs bind <instance> [workspace]
  node .cursor/runtime/thoughts.mjs unbind [workspace]
  node .cursor/runtime/thoughts.mjs schedule [workspace] <delayMs> [reason]
  node .cursor/runtime/thoughts.mjs record-user [workspace] [preview]
  node .cursor/runtime/thoughts.mjs record-active [workspace] [mode] [topic]
  node .cursor/runtime/thoughts.mjs select-thought [workspace]
  node .cursor/runtime/thoughts.mjs consume-thought [workspace] <candidateId> [mode] [topic]
  node .cursor/runtime/thoughts.mjs dry-run-active [workspace]
  node .cursor/runtime/thoughts.mjs context [workspace]
  node .cursor/runtime/thoughts.mjs set-permission <instance> <signal> <always|ask|deny>
  node .cursor/runtime/thoughts.mjs state [workspace]
  node .cursor/runtime/thoughts.mjs validate-mind-state [workspace|instance]
  node .cursor/runtime/thoughts.mjs validate-thought-state [workspace|instance]
  node .cursor/runtime/thoughts.mjs notify <title> <subtitle> <message>`);
    }
} catch (error) {
    console.error(error.message);
    process.exit(1);
}
