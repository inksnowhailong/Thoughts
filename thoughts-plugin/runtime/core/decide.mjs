// 思绪运行时 — 决策层
// 纯函数：根据循环状态 + 用户画像，判断「现在该不该主动说话」以及「下次间隔多久」。
// 不依赖任何宿主，可被 daemon、hook、或单元测试直接调用。
// 这是动态调频的核心：把原本写在 Claude Cron instructions 里的判断逻辑收敛成代码。

import { selectMode } from './mind.mjs';
import { beijingHour } from './clock.mjs';

/** 各档间隔（毫秒），供动态调频选用 */
const INTERVAL = {
    eager: 10 * 60 * 1000, // 趁热打铁
    base: 15 * 60 * 1000, // 默认
    cooling: 30 * 60 * 1000, // 一次没回
    cold: 60 * 60 * 1000, // 连续没回
    rest: 120 * 60 * 1000, // 深夜/勿扰
};

/**
 * 判断当前时间是否落在休息时段。
 * @param {number} hour 当前小时(0-23)
 * @param {number[]} quietHours 休息时段，形如 [23, 7] 表示 23 点到次日 7 点
 */
function isQuietHour(hour, quietHours) {
    if (!Array.isArray(quietHours) || quietHours.length !== 2) {
        // 默认 23:00 - 07:00 为休息时段
        return hour >= 23 || hour < 7;
    }
    const [start, end] = quietHours;
    return start <= end ? hour >= start && hour < end : hour >= start || hour < end;
}

/**
 * 硬闸门：纯代码红线，命中即给结果、绝不调模型（也不该调）。
 * 只放两条不可商量的红线——休息时段、用户正在打字——其余交给上层的便宜模型软判断。
 * @param {object} loopState 循环状态(lastUserAt)
 * @param {object} profile 用户画像(可含 habits.quietHours)
 * @param {number} now 当前时间戳
 * @returns {null | { act: boolean, reason: string, nextDelayMs: number, mode: string }} 放行返回 null
 */
export function hardGate(loopState = {}, profile = {}, now = Date.now()) {
    const hour = beijingHour(now); // 作息红线按北京时间判断
    const lastUserAt = Number(loopState?.lastUserAt || 0);

    // 休息时段：不打扰，拉长间隔（边界，绝对优先）
    if (isQuietHour(hour, profile?.habits?.quietHours)) {
        return {
            act: false, reason: 'quiet_hour', nextDelayMs: INTERVAL.rest, mode: 'quiet',
        };
    }
    // 用户刚刚交互过(5 分钟内)：不抢话，短间隔后再看
    if (now - lastUserAt < 5 * 60 * 1000) {
        return {
            act: false, reason: 'user_just_active', nextDelayMs: INTERVAL.eager, mode: 'quiet',
        };
    }
    return null;
}

/**
 * 决策主动行为（纯代码版）：作为便宜模型软判断不可用时的优雅降级兜底。
 * 是否开口的"调频闸门"在此；说什么类型由 mind.selectMode 按权重+多样性挑。
 * @param {object} loopState 循环状态(lastUserAt/lastActiveAt/consecutiveNoReply)
 * @param {object} profile 用户画像(可含 habits.quietHours)
 * @param {object} mindState 心智状态(供挑选行为模式)
 * @param {number} now 当前时间戳(默认 Date.now())
 * @returns {{ act: boolean, reason: string, nextDelayMs: number, mode: string }}
 */
export function decideActive(loopState, profile = {}, mindState = {}, now = Date.now()) {
    // 先过红线
    const gated = hardGate(loopState, profile, now);
    if (gated) return gated;

    const lastUserAt = Number(loopState?.lastUserAt || 0);
    const noReply = Number(loopState?.consecutiveNoReply || 0);

    // 连续 3 次无人回复：明显不在状态，冷却
    if (noReply >= 3) {
        return {
            act: false, reason: 'too_many_no_reply', nextDelayMs: INTERVAL.cold, mode: 'quiet',
        };
    }

    // 决定开口 → 按心智挑一个行为模式
    const mode = selectMode(mindState);

    // 用户在 5-30 分钟前刚聊过：趁热打铁
    if (now - lastUserAt < 30 * 60 * 1000) {
        return { act: true, reason: 'recent_chat', nextDelayMs: INTERVAL.eager, mode };
    }

    // 一次没回：适当放慢
    if (noReply >= 1) {
        return { act: true, reason: 'one_no_reply', nextDelayMs: INTERVAL.cooling, mode };
    }

    return { act: true, reason: 'normal', nextDelayMs: INTERVAL.base, mode };
}

/**
 * 决策潜意识间隔：用户越活跃整理越勤，空闲则放慢省 token。
 * 潜意识总是执行（只是频率不同），所以只返回间隔。
 * @param {object} loopState 循环状态
 * @param {object} profile 用户画像
 * @param {number} now 当前时间戳
 * @returns {{ nextDelayMs: number, reason: string }}
 */
export function decideSubconscious(loopState, profile = {}, now = Date.now()) {
    const hour = beijingHour(now); // 作息按北京时间
    const lastUserAt = Number(loopState?.lastUserAt || 0);
    const idleMs = now - lastUserAt;

    if (isQuietHour(hour, profile?.habits?.quietHours)) {
        return { nextDelayMs: INTERVAL.cold, reason: 'quiet_hour' };
    }
    if (idleMs < 30 * 60 * 1000) {
        return { nextDelayMs: INTERVAL.eager, reason: 'active_user' };
    }
    if (idleMs > 60 * 60 * 1000) {
        return { nextDelayMs: INTERVAL.cold, reason: 'idle_user' };
    }
    return { nextDelayMs: 20 * 60 * 1000, reason: 'default' };
}
