/**
 * 运行时配置加载。
 *
 * - runtime.json：端口 / Python 桥地址 / 鉴权 token（真实文件 gitignore，提交 runtime.example.json）；
 * - models.json：角色→模型（main / worker / screen_vlm），真实文件 gitignore，提交 models.example.json；
 * - 全部字段支持环境变量覆盖（AGENT_RUNTIME_*），env 优先于文件。
 *
 * token 是 agent-runtime 的 Bearer 鉴权令牌（语音侧 GUANJIA_GATEWAY_TOKEN 同值），
 * 生成方式：随机 hex（见 runtime.example.json 的说明）。
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { AGENT_RUNTIME_ROOT } from './tools/paths.js';

export interface RuntimeConfig {
  port: number;
  bridgeUrl: string;
  token: string;
}

export type ModelRole = 'main' | 'worker' | 'screen_vlm';

export interface ModelRoleConfig {
  provider: string;
  model: string;
  baseUrl: string;
  apiKey: string;
}

export interface ModelConfig {
  roles: Record<ModelRole, ModelRoleConfig>;
}

function pkgRoot(): string {
  // src 与 dist 两种布局都要定位到包根（找 package.json 的稳定特征，见 tools/paths.ts）
  return AGENT_RUNTIME_ROOT;
}

function readJsonFile(file: string): Record<string, unknown> {
  // 剥 BOM：Windows PowerShell 的 UTF8 默认带 BOM，Node JSON.parse 会炸（同 2026-10-27 Python 侧事故）
  const raw = readFileSync(file, 'utf8');
  return JSON.parse(raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw) as Record<string, unknown>;
}

function strOr(v: unknown, fallback: string): string {
  return typeof v === 'string' && v.length > 0 ? v : fallback;
}

function intOr(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isInteger(v) && v > 0 ? v : fallback;
}

function env(name: string): string | undefined {
  const v = process.env[name];
  return typeof v === 'string' && v.length > 0 ? v : undefined;
}

/** 加载 runtime.json（缺省字段用默认值），env 覆盖：AGENT_RUNTIME_PORT / _BRIDGE_URL / _TOKEN。 */
export function loadRuntimeConfig(): RuntimeConfig {
  const file = path.join(pkgRoot(), 'config', 'runtime.json');
  const raw = existsSync(file) ? readJsonFile(file) : {};
  return {
    port: env('AGENT_RUNTIME_PORT') ? intOr(Number(env('AGENT_RUNTIME_PORT')), 18800) : intOr(raw.port, 18800),
    bridgeUrl: env('AGENT_RUNTIME_BRIDGE_URL') ?? strOr(raw.bridgeUrl, 'http://127.0.0.1:18801'),
    token: env('AGENT_RUNTIME_TOKEN') ?? strOr(raw.token, ''),
  };
}

const ROLE_ENV_PREFIX: Record<ModelRole, string> = {
  main: 'AGENT_RUNTIME_MAIN',
  worker: 'AGENT_RUNTIME_WORKER',
  screen_vlm: 'AGENT_RUNTIME_SCREEN_VLM',
};

/**
 * 加载 models.json（文件必须存在，可从 models.example.json 复制），
 * 每个角色支持 env 覆盖：AGENT_RUNTIME_<ROLE>_MODEL / _BASE_URL / _API_KEY / _PROVIDER。
 */
export function loadModelConfig(): ModelConfig {
  const file = path.join(pkgRoot(), 'config', 'models.json');
  if (!existsSync(file)) {
    throw new Error(`缺少 ${file}（复制 models.example.json 为 models.json 并填入 apiKey）`);
  }
  const raw = readJsonFile(file) as { roles?: Record<string, Record<string, unknown>> };
  const roles = {} as Record<ModelRole, ModelRoleConfig>;
  for (const role of Object.keys(ROLE_ENV_PREFIX) as ModelRole[]) {
    const r = raw.roles?.[role];
    if (!r) throw new Error(`models.json 缺少 roles.${role}`);
    const p = ROLE_ENV_PREFIX[role];
    roles[role] = {
      provider: env(`${p}_PROVIDER`) ?? strOr(r.provider, ''),
      model: env(`${p}_MODEL`) ?? strOr(r.model, ''),
      baseUrl: env(`${p}_BASE_URL`) ?? strOr(r.baseUrl, ''),
      apiKey: env(`${p}_API_KEY`) ?? strOr(r.apiKey, ''),
    };
  }
  return { roles };
}
