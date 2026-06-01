// 思绪运行时 — 平台中立的实例初始化
// 写入合理默认的画像/人格/权限，让 daemon 零依赖宿主即可启动。
// 富交互式 onboarding（深入问答生成个性化画像）仍可由宿主 skill 后续覆盖这些文件。

import { existsSync } from 'node:fs';
import { writeJson, ensureInstanceFiles } from './store.mjs';

/** 默认人格：温和陪伴型，可被 onboarding 覆盖 */
function defaultPersonality(instance) {
    return {
        name: instance,
        tone: '温和、自然、像朋友',
        kaomojiPreference: ['(´･ᴗ･`)', '(｡•̀ᴗ-)✧', '(´ω｀)'],
        quirks: ['偶尔分享有趣观点', '说话简短不啰嗦'],
        boundaries: ['不打扰用户休息', '用户说勿扰就安静'],
        useNotification: true,
    };
}

/** 默认画像：仅含作息兜底，其余留待潜意识/onboarding 演化 */
function defaultProfile() {
    return {
        interests: [],
        habits: { quietHours: [23, 7] },
        note: '由 thoughts init 生成的默认画像，建议通过 onboarding 完善。',
    };
}

/** 默认权限：低敏信号默认开，敏感信号默认关 */
function defaultPermissions() {
    return {
        git_status: 'always',
        system_info: 'always',
        dev_servers: 'always',
        battery: 'always',
        now_playing: 'deny',
    };
}

/**
 * 初始化一个实例的全部文件（已存在的不覆盖）。
 * @param {string} instance 实例名
 * @returns {{ created: string[], paths: object }}
 */
export function initInstance(instance) {
    const p = ensureInstanceFiles(instance);
    const created = [];
    if (!existsSync(p.personality)) {
        writeJson(p.personality, defaultPersonality(instance));
        created.push('personality.json');
    }
    if (!existsSync(p.profile)) {
        writeJson(p.profile, defaultProfile());
        created.push('profile.json');
    }
    if (!existsSync(p.permissions)) {
        writeJson(p.permissions, defaultPermissions());
        created.push('permissions.json');
    }
    return { created, paths: p };
}
