/**
 * screen_understand 用的视觉模型入口。
 *
 * TODO(切换): 并行代理 A 的 src/models/provider.ts getChatModel('screen_vlm') 就绪后，
 * 删除本文件的本地回退实现，改为直接 re-export。在那之前按以下顺序取配置：
 *   1. 动态 import '../models/provider.js'（存在即用，A 交付后自动生效）；
 *   2. agent-runtime/config/models.json 的 roles.screen_vlm
 *      {model, baseURL, apiKey}（config 由并行代理 A 维护，密钥不入库）；
 *   3. 环境变量 GUANJIA_SCREEN_VLM_MODEL / GUANJIA_SCREEN_VLM_BASE_URL / GUANJIA_SCREEN_VLM_API_KEY。
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { ChatOpenAI } from '@langchain/openai';
import type { BaseChatModel } from '@langchain/core/language_models/chat_models';
import { CONFIG_DIR } from './paths.js';

export interface VlmModelConfig {
  model: string;
  baseURL?: string;
  apiKey?: string;
}

/** 读 agent-runtime/config/models.json 里的 screen_vlm 角色配置（文件不存在返回 null）。 */
export function readScreenVlmConfig(): VlmModelConfig | null {
  const file = path.join(CONFIG_DIR, 'models.json');
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
  if (raw === null || typeof raw !== 'object') return null;
  const roles = (raw as Record<string, unknown>).roles;
  if (roles === null || typeof roles !== 'object') return null;
  const role = (roles as Record<string, unknown>).screen_vlm;
  if (role === null || typeof role !== 'object') return null;
  const record = role as Record<string, unknown>;
  const model = typeof record.model === 'string' ? record.model : '';
  if (!model) return null;
  // 兼容 baseUrl / baseURL 两种拼写（与并行代理 A 的 config/models.json 对齐）
  const baseURL = typeof record.baseURL === 'string' ? record.baseURL
    : typeof record.baseUrl === 'string' ? record.baseUrl : undefined;
  const apiKey = typeof record.apiKey === 'string' ? record.apiKey : undefined;
  return { model, baseURL, apiKey };
}

function fromEnv(): VlmModelConfig | null {
  const model = process.env.GUANJIA_SCREEN_VLM_MODEL;
  if (!model || !model.trim()) return null;
  return {
    model,
    baseURL: process.env.GUANJIA_SCREEN_VLM_BASE_URL,
    apiKey: process.env.GUANJIA_SCREEN_VLM_API_KEY,
  };
}

function buildModel(config: VlmModelConfig): BaseChatModel {
  if (!config.apiKey) {
    throw new Error('screen_vlm 模型缺少 apiKey（GUANJIA_SCREEN_VLM_API_KEY 或 config/models.json）');
  }
  return new ChatOpenAI({
    model: config.model,
    apiKey: config.apiKey,
    configuration: config.baseURL ? { baseURL: config.baseURL } : undefined,
    maxRetries: 1,
    // 视觉模型对大截图的首响应常超 60s，给足余量；截屏本身另有 15s 捕获超时。
    timeout: 120_000,
  });
}

/** 取 screen_vlm 视觉模型；三处配置都缺时抛错（宁硬勿静默）。 */
export async function getScreenVlmChatModel(): Promise<BaseChatModel> {
  // 0) 环境变量显式覆盖：运维逃生口，最高优先（冒烟/临时切换不改配置文件）
  const fromEnvironment = fromEnv();
  if (fromEnvironment !== null) return buildModel(fromEnvironment);
  // 1) 并行代理 A 的 provider 模块（交付后自动接管）
  try {
    const mod = (await import('../models/provider.js')) as {
      getChatModel?: (role: string) => BaseChatModel;
    };
    if (typeof mod.getChatModel === 'function') {
      return mod.getChatModel('screen_vlm');
    }
  } catch {
    // 模块尚未就绪 → 走本地回退
  }
  // 2) config/models.json 本地副本（A 未就绪时的过渡；TODO: A 交付后删除）
  const config = readScreenVlmConfig();
  if (config === null) {
    throw new Error(
      'screen_vlm 模型未配置：请在 agent-runtime/config/models.json 配置 roles.screen_vlm，' +
        '或设置 GUANJIA_SCREEN_VLM_MODEL / GUANJIA_SCREEN_VLM_BASE_URL / GUANJIA_SCREEN_VLM_API_KEY',
    );
  }
  return buildModel(config);
}
