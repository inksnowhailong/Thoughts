// 思绪运行时 — 对话热度模型（heat-driven cadence）
// 纯函数、零依赖：把"下次什么时候主动说话"从固定节拍变成随对话实时演化的状态量。
//
// 核心思想：
//   heat ∈ [0,1] 表示对话热度，用户每说一句加热，随时间指数衰减；
//   下次开口时间 nextSpeakAt 由 heat 决定——越热间隔越短（4min 起），越冷越长（75min 封顶）。
//   用户消息只能把 nextSpeakAt 拉近、永不推远（min 合并）；
//   说话本身不加热（自嗨不算热度），说完按当时有效热度排下一次。
//
// 存储约定（loop-state.json 增量字段）：
//   heat        上次写入时刻的热度值
//   heatAt      该热度对应的时间戳（ms）——衰减发生在"读取时"而非写入时
//   nextSpeakAt 下次允许主动开口的时间戳（ms），gate 的唯一限速来源

/** 热度半衰期：25 分钟没新消息，热度减半 */
const HALF_LIFE_MS = 25 * 60 * 1000;
/** 单条用户消息的加热量 */
const BUMP = 0.3;
/** 最短开口间隔（热聊时） */
const MIN_INTERVAL_MS = 4 * 60 * 1000;
/** 最长开口间隔（凉透时），保持存在感、绝不静默退避 */
const MAX_INTERVAL_MS = 75 * 60 * 1000;

/**
 * 读取此刻的有效热度：存储值按经过时间做指数衰减。
 * @param {object} loopState 含 heat/heatAt 的循环状态
 * @param {number} now 当前时间戳
 * @returns {number} 有效热度 [0,1]
 */
export function effectiveHeat(loopState = {}, now = Date.now()) {
    const heat = Number(loopState.heat || 0);
    const heatAt = Number(loopState.heatAt || 0);
    if (heat <= 0 || heatAt <= 0) return 0;
    const dt = Math.max(0, now - heatAt);
    return heat * Math.exp(-dt * Math.LN2 / HALF_LIFE_MS);
}

/**
 * 按热度算下次开口间隔：interval = max(4min, 75min × (1-heat)²) × 抖动(0.8~1.2)。
 * 抖动让节奏不可预测——人不会掐着表说话。
 * @param {number} heat 有效热度
 * @param {() => number} rand 随机源（默认 Math.random，测试可注入）
 * @returns {number} 间隔毫秒数
 */
export function intervalMs(heat, rand = Math.random) {
    const base = Math.max(MIN_INTERVAL_MS, MAX_INTERVAL_MS * (1 - heat) ** 2);
    return Math.round(base * (0.8 + rand() * 0.4));
}

/**
 * 用户说了一句话：加热 + 把 nextSpeakAt 拉近（只拉近、永不推远）。
 * 直接原地修改 loopState 并返回它（调用方负责落盘）。
 * @param {object} loopState 循环状态
 * @param {number} now 当前时间戳
 */
export function bumpHeat(loopState = {}, now = Date.now()) {
    const heat = Math.min(1, effectiveHeat(loopState, now) + BUMP);
    loopState.heat = heat;
    loopState.heatAt = now;
    const candidate = now + intervalMs(heat);
    const current = Number(loopState.nextSpeakAt || 0);
    loopState.nextSpeakAt = current > now ? Math.min(current, candidate) : candidate;
    return loopState;
}

/**
 * 刚主动说完一句：按此刻有效热度排下一次开口时间（说话不加热）。
 * 直接原地修改 loopState 并返回它（调用方负责落盘）。
 * @param {object} loopState 循环状态
 * @param {number} now 当前时间戳
 */
export function scheduleNext(loopState = {}, now = Date.now()) {
    loopState.nextSpeakAt = now + intervalMs(effectiveHeat(loopState, now));
    return loopState;
}

/**
 * 热度分层 → 内容策略：hot 贴话题接茬 / warm 半贴半飘 / cold 聊自己的线索。
 * @param {number} heat 有效热度
 * @returns {'hot'|'warm'|'cold'}
 */
export function heatTier(heat) {
    if (heat >= 0.6) return 'hot';
    if (heat >= 0.2) return 'warm';
    return 'cold';
}
