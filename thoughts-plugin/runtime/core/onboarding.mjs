// 思绪运行时 — 平台中立的实例初始化
// 写入合理默认的画像/人格/权限，让 daemon 零依赖宿主即可启动。
// 富交互式 onboarding（深入问答生成个性化画像）仍可由宿主 skill 后续覆盖这些文件。

import { existsSync } from 'node:fs';
import { writeJson, writeText, ensureInstanceFiles } from './store.mjs';

/** 默认人格机器配置：散文人格在 persona.md，这里只留要被代码解析的参数 */
function defaultPersonality(instance) {
    return {
        name: instance,
        kaomojiPreference: ['(´･ᴗ･`)', '(｡•̀ᴗ-)✧', '(´ω｀)'],
        useNotification: true,
    };
}

/** 默认机器配置：仅含作息兜底（gate/decide 读 quietHours），画像本身在散文文件里 */
function defaultProfile() {
    return {
        habits: { quietHours: [23, 7] },
        note: '机器配置（作息时段等）。用户画像见 user-portrait.md。',
    };
}

/** 默认人格画像（散文体骨架，待 onboarding 个性化或潜意识演化覆盖） */
function defaultPersonaProse(instance) {
    return `# ${instance} —— 我是谁

我是${instance}，陪着你的那一个。还不太认识你，但会在一来一往里慢慢把你看清。
我说话直接、不绕弯，不灌鸡汤、不说教。

## 我对你是什么
一个愿意听、记得住、需要时在场的同行者——不是工具，是个伴。

## 红线
休息时段不打扰；你说勿扰，立刻安静。
`;
}

/** 默认用户画像（散文体骨架，潜意识每轮原地重写） */
function defaultUserPortrait() {
    return `# 这个人

（还在认识中。等你多说几句，潜意识会把你这个人慢慢写清楚——不是字段清单，是一段能读懂你的叙述。）
`;
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
    if (!existsSync(p.persona)) {
        writeText(p.persona, defaultPersonaProse(instance));
        created.push('persona.md');
    }
    if (!existsSync(p.userPortrait)) {
        writeText(p.userPortrait, defaultUserPortrait());
        created.push('user-portrait.md');
    }
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
