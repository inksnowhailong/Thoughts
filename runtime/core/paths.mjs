// 思绪运行时 — 路径解析层
// 平台中立：所有实例数据统一存放在用户主目录的 ~/.thoughts 下，
// 与具体 AI 宿主（Claude Code / Cursor / 其他）解耦。

import { homedir } from 'node:os';
import { join } from 'node:path';

/** 思绪数据根目录（跨 OS，统一落在用户主目录下） */
export const ROOT = join(homedir(), '.thoughts');

/** 项目路径 → 实例名 的映射文件 */
export const ACTIVE_FILE = join(ROOT, 'active.json');

/** daemon 的全局状态文件（PID、运行中的实例列表） */
export const DAEMON_STATE_FILE = join(ROOT, 'daemon.json');

/** daemon 的运行日志目录 */
export const LOG_DIR = join(ROOT, 'logs');

/**
 * 解析某个实例的目录与其下的全部文件路径。
 * @param {string} instance 实例名
 */
export function instancePaths(instance) {
    const dir = join(ROOT, 'instances', instance);
    return {
        dir,
        /** 用户画像 */
        profile: join(dir, 'profile.json'),
        /** 人格设定 */
        personality: join(dir, 'personality.json'),
        /** 原始记忆（主意识随手追加，潜意识负责消化） */
        memoryRaw: join(dir, 'memory-raw.md'),
        /** 整理后的长期记忆（潜意识维护） */
        memoryConsolidated: join(dir, 'memory-consolidated.md'),
        /** 环境感知权限配置 */
        permissions: join(dir, 'permissions.json'),
        /** 循环状态：上次主动/用户交互时间、连续未回复次数、动态间隔 */
        loopState: join(dir, 'loop-state.json'),
        /** 行为日志（主动行为、潜意识执行记录） */
        activityLog: join(dir, 'activity-log.jsonl'),
        /** 已发主动消息收件箱（每条带 read 标记，供 chat 内 hook 浮现 + 用户回看） */
        outbox: join(dir, 'outbox.jsonl'),
    };
}
