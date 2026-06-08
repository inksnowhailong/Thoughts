// 思绪运行时 — 统一时钟（北京时间 / UTC+8）
// 为什么要它：模型对"现在几点"的判断必须确定且统一为北京时间——
// 不能依赖机器时区，也不能让 toISOString() 的 UTC 时间戳混进模型视野（会把上午 9:45 误判成凌晨 1:45）。
// 凡是"会被模型读到的时间"与"作息红线判断"，一律走这里。

/** 北京相对 UTC 的固定偏移（中国全境不实行夏令时，恒为 +8） */
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;

/** 把时间戳换算成北京"墙上时间"的各分量（用 getUTC* 读取已偏移后的值） */
export function beijingParts(now = Date.now()) {
    const d = new Date(now + BEIJING_OFFSET_MS);
    return {
        year: d.getUTCFullYear(),
        month: d.getUTCMonth() + 1,
        day: d.getUTCDate(),
        hour: d.getUTCHours(),
        minute: d.getUTCMinutes(),
        second: d.getUTCSeconds(),
        weekday: d.getUTCDay(), // 0=周日
    };
}

/** 北京时间的小时(0-23)——作息红线判断用 */
export function beijingHour(now = Date.now()) {
    return beijingParts(now).hour;
}

/** 带 +08:00 后缀的 ISO 时间戳——写进 raw/记忆，让模型一眼看出是北京时间 */
export function beijingStamp(now = Date.now()) {
    const p = beijingParts(now);
    const z = (n) => String(n).padStart(2, '0');
    return `${p.year}-${z(p.month)}-${z(p.day)}T${z(p.hour)}:${z(p.minute)}:${z(p.second)}+08:00`;
}

/** 人话标签，如 "2026-06-04 09:45 周三（北京时间）"——喂给环境快照供模型判断当下 */
export function beijingLabel(now = Date.now()) {
    const p = beijingParts(now);
    const z = (n) => String(n).padStart(2, '0');
    const wd = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][p.weekday];
    return `${p.year}-${z(p.month)}-${z(p.day)} ${z(p.hour)}:${z(p.minute)} ${wd}（北京时间）`;
}
