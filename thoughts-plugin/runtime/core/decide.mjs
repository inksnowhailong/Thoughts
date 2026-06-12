// 思绪运行时 — 决策层
// 纯函数：判断潜意识整理记忆的节奏（用户越活跃整理越勤，空闲则放慢省 token）。
// 不依赖任何宿主，可被潜意识运行器或单元测试直接调用。
// （主动开口的决策已收敛到 heat 模型 + cli `gate`，不再走这里。）

import { beijingHour } from './clock.mjs';

/** 潜意识间隔档位（毫秒） */
const INTERVAL = {
    eager: 10 * 60 * 1000, // 用户活跃，勤整理
    cold: 60 * 60 * 1000, // 深夜/久未互动，放慢省 token
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
