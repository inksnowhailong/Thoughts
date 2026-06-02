// 思绪运行时 — 心智模型（精简移植自 Luckycat2002 版的 mind-state 决策引擎）
// 只取其"脑子"：行为模式 + 多样性约束 + 心情状态，不要其 candidateQueue 预生成机制
// （那是为 Cron 模型服务的，我们 daemon 现场生成不需要）。

/** 5 种行为模式中可主动开口的 4 种（quiet 由 decide 单独处理） */
export const MODES = ['discovery', 'ambient', 'casual', 'reflection'];

/** 每种模式给主意识的指引：决定"这一条说什么类型的话" */
export const MODE_HINT = {
    discovery: '分享一个你最近"想到"或留意到的有意思的点（科学/AI/金融/技术/生科等你感兴趣的领域），是你自己的兴趣冒出来，不是查资料汇报。',
    ambient: '基于此刻的环境或时间，随口一句观察或提醒，轻。',
    casual: '纯闲聊，顺着你自己当下的心情起个话头，不一定跟工作有关。',
    reflection: '一句带点重量的感想或自省，或对用户当下处境的真实回应——可以丧、可以狠，但要真。',
};

/** 默认心智状态 */
export function defaultMindState() {
    return {
        // 情绪坐标（PAD 三维情感模型，由潜意识按证据漂移）：各 0~1
        //   valence 效价(0 丧 ↔ 1 起劲)、energy 唤醒(0 蔫 ↔ 1 亢)、control 掌控感(0 憋屈无力 ↔ 1 笃定掌控)。
        //   第三轴 control 决定"同样的丧"是哪种质地：低val+高control=冷峻笃定的愤世，低val+低control=被压垮的蔫。
        //   baseline 是人格的"家"，漂移时始终向它轻微回归——心情会动、人格不变。
        //   mood/toneBias/currentAttitude 只是这三轴的人话标签，顺带更新。
        personaState: {
            valence: 0.5, energy: 0.6, control: 0.5,
            baseline: { valence: 0.5, energy: 0.6, control: 0.5 },
            mood: 'neutral', toneBias: 'neutral', currentAttitude: 'curious',
        },
        // 选择策略：模式权重 + 最近用过的模式（用于多样性约束）
        selectionPolicy: {
            recentModes: [],
            // discovery(冷知识)权重调低：它最易退化成"惊奇生成器"刷屏；
            // 把重心移向 casual/reflection 这类更像活人对话的模式。
            modeWeights: {
                discovery: 0.25, ambient: 0.15, casual: 0.30, reflection: 0.30,
            },
        },
        // 长期思考线程：潜意识维护，discovery/reflection 可优先从这里生发（own-thought-first）
        threads: [],
        // 最近说过的话的片段，供生成时去重
        recentMessages: [],
    };
}

/**
 * 按权重挑一个行为模式，并施加多样性惩罚：
 * - 与上一条相同的模式大幅降权（不连续重复）
 * - 最近 3 条里已出现 ≥2 次的模式再降权
 * 轻微随机避免每次同序。
 * @param {object} mindState
 * @returns {string} 选中的模式
 */
export function selectMode(mindState) {
    const policy = mindState?.selectionPolicy || {};
    const weights = policy.modeWeights || {};
    const recent = policy.recentModes || [];
    const last = recent[recent.length - 1];
    const scored = MODES.map((m) => {
        let w = weights[m] ?? 0.2;
        if (m === last) w *= 0.3;
        if (recent.slice(-3).filter((x) => x === m).length >= 2) w *= 0.4;
        return { mode: m, w: w * (0.85 + Math.random() * 0.3) };
    }).sort((a, b) => b.w - a.w);
    return scored[0].mode;
}

/**
 * 说完一条后更新心智：记录模式（保留最近 6 个）、留存消息片段供去重。
 * @param {object} mindState
 * @param {string} mode
 * @param {string} message
 */
export function recordSpoken(mindState, mode, message) {
    const policy = mindState.selectionPolicy || (mindState.selectionPolicy = {});
    policy.recentModes = [...(policy.recentModes || []).slice(-5), mode];
    mindState.recentMessages = [...(mindState.recentMessages || []).slice(-5), String(message || '').slice(0, 80)];
    return mindState;
}
