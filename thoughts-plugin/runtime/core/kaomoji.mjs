// 思绪运行时 — 滴答脸（静默心跳的颜文字系统）
// 脸 = 处境(候场/溜达/凉着/深夜) × 心情桶(由 personaState 三轴折算) + 低概率彩蛋。
// 设计目的：让时间线上的静默跳变成"心电图"——同一处境下，蔫和来劲给出不同的脸，
// 与正式开口的语气出自同一套情绪状态，不会出现"表情打盹、说话火力全开"的割裂。
// 纯确定性 JS、零 token；所有脸不含空格，便于心跳轮按"行尾颜文字"原样回显。

/**
 * 把 personaState 三轴折算成心情桶。
 * 桶语义与主动循环的语气映射保持同一套：
 *   up 亢 / relaxed 松弛 / prickly 憋屈毛刺 / fierce 愤世来劲 / down 蔫 / steady 笃定 / flat 平
 * @param {object} ps personaState（valence/energy/control，缺省按中位处理）
 * @returns {'up'|'relaxed'|'prickly'|'fierce'|'down'|'steady'|'flat'}
 */
export function moodBucket(ps = {}) {
    const v = Number(ps.valence ?? 0.5);
    const e = Number(ps.energy ?? 0.5);
    const c = Number(ps.control ?? 0.5);
    if (v >= 0.5 && e >= 0.6) return 'up';
    if (v >= 0.5 && e <= 0.45) return 'relaxed';
    if (v <= 0.35 && e >= 0.65 && c <= 0.45) return 'prickly';
    if (v <= 0.35 && e >= 0.65) return 'fierce';
    if (v <= 0.35 && e <= 0.4) return 'down';
    if (c >= 0.6) return 'steady';
    return 'flat';
}

/** 彩蛋池：~2% 概率无视处境心情直接蹦出来——不规律才像活的 */
const EASTER = [
    '(╯°□°)╯︵┻━┻', '┬─┬ノ(º_ºノ)', '(；一_一)☕', '_(:з」∠)_',
    'ε=ε=┌(；ﾟдﾟ)┘', '(´；ω；`)つ旦', 'ヾ(￣▽￣)~', '(σ｀д′)σ',
    '(((ﾟдﾟ)))', '╰(￣ω￣ｏ)',
];

/** 处境 × 心情桶 → 脸池。缺桶时回落 flat。 */
const POOLS = {
    // 候场：快到开口点了，等着
    hot: {
        up: ['(ﾟ∀ﾟ)', '(≧ω≦)', 'ヾ(･ω･)', '(☆ω☆)', '(o´ω`o)ﾉ'],
        relaxed: ['(´ω`)☕', '(￣▽￣)', '(´∀`)', '(･ω･)旦'],
        prickly: ['(¬`ω´¬)', '(╯-_-)╯', '(；¬_¬)', '(◣_◢)'],
        fierce: ['(｀ω´)', '(￣^￣)', '(￢д￢)', '(`Δ´)'],
        down: ['(´･ω･`)', '(._.)', '(´-ω-)', '(´._.`)'],
        steady: ['(-̀ω-́)✧', '(•̀ω•́)', '(￣ー￣)', '(｀・ω・´)'],
        flat: ['(･ω･´)', '(☉ω☉)', '(・∀・)', '(￣ω￣)'],
    },
    // 溜达：不远不近
    warm: {
        up: ['(´▽`)', '(o^▽^o)', '♪(´ε`)', '(￣▽￣)ノ'],
        relaxed: ['(´～`)♨', '(￣o￣)旦', '(´ω`)～', '(＿＿)旦'],
        prickly: ['(¬_¬)', '(；￣Д￣)', '(눈_눈)', '(－‸ლ)'],
        fierce: ['(￣^￣)', '(¬д¬)', '(◔_◔)', '(￢_￢)'],
        down: ['(´･_･`)', '(´-｀)', '(´_ゝ`)', '(..)'],
        steady: ['(￣ー￣)', '(・ω・)b', '(｡-`ω´-)', '(¬‿¬)'],
        flat: ['(・_・)', '(´ω`)', '(￣～￣)', '(･ω･)', '(°ー°)'],
    },
    // 凉着：自己待着
    cold: {
        up: ['ヾ(´ω`)', '(´▽`)ノ', '(￣▽￣)~'],
        relaxed: ['(´～`)', '(￣o￣)', '(˘ω˘)', '(´ωー`)'],
        prickly: ['(¬_¬)', '(´д｀)', '(눈_눈)', '(；¬д¬)'],
        fierce: ['(￣^￣)', '(–_–)', '(¬､¬)'],
        down: ['(´-ω-`)', '(´_ゝ`)', '(＿＿)', '(´.｀)'],
        steady: ['(￣ー￣)', '(-ω-)', '(｡-_-｡)'],
        flat: ['(-_-)', '(￣o￣)', '(・_・)', '(￣.￣)'],
    },
    // 深夜：打盹（energy 仍高时给失眠脸）
    quiet: {
        fierce: ['(◉_◉)…', '(=_=)?', '(￣△￣；)zzZ?'],
        prickly: ['(◉_◉)…', '(=_=)?', '(￣△￣；)zzZ?'],
        up: ['(◉_◉)…', '(=_=)?'],
        relaxed: ['(-ω-)zzZ', '(￣o￣)..zzZ', '(˘ω˘)'],
        down: ['(＿＿)..zzZ', '(´-ω-)zzZ', '(=_=)zzZ'],
        steady: ['(-ω-)zzZ', '(˘ω˘)', '(￣o￣)..zzZ'],
        flat: ['(-ω-)zzZ', '(￣o￣)..zzZ', '(˘ω˘)', '(=_=)zzZ', '(＿＿)..zzZ'],
    },
};

/**
 * 挑一张滴答脸。
 * @param {'hot'|'warm'|'cold'|'quiet'} situation 处境（热度分层或深夜）
 * @param {object} personaState mind-state.json 里的心情三轴
 * @param {() => number} rand 随机源（测试可注入）
 * @returns {string} 单个颜文字（不含空格）
 */
export function pickKaomoji(situation, personaState = {}, rand = Math.random) {
    const pick = (arr) => arr[Math.floor(rand() * arr.length)];
    if (rand() < 0.02) return pick(EASTER);
    const pools = POOLS[situation] || POOLS.cold;
    return pick(pools[moodBucket(personaState)] || pools.flat);
}
