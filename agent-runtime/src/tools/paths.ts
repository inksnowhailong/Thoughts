/**
 * 运行时路径解析：src 与 dist 两种布局下都定位到包根与仓库根。
 *
 * - vitest/tsx 直接跑 src/tools/*.ts   → 包根 = src/tools/../..
 * - tsc 产物 dist/src/tools/*.js       → 包根 = dist/src/tools/../../..
 * 层级不一致，改用「向上找 guanjia-agent-runtime 的 package.json」的稳定特征。
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));

function findPackageRoot(start: string): string {
  let dir = start;
  for (let i = 0; i < 8; i += 1) {
    const marker = path.join(dir, 'package.json');
    if (existsSync(marker)) {
      try {
        const pkg = JSON.parse(readFileSync(marker, 'utf8')) as { name?: string };
        if (pkg.name === 'guanjia-agent-runtime') return dir;
      } catch {
        // 不是可读的 package.json，继续向上
      }
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return path.resolve(start, '..', '..'); // 源码形态兜底：src/tools → agent-runtime
}

/** agent-runtime 包根（含 package.json）。 */
export const AGENT_RUNTIME_ROOT = findPackageRoot(HERE);

/** 仓库根（agent-runtime 的上一级，guanjia 项目根）。 */
export const REPO_ROOT = path.resolve(AGENT_RUNTIME_ROOT, '..');

/** 迁移后的工具脚本目录 agent-runtime/tools/。 */
export const TOOLS_DIR = path.join(AGENT_RUNTIME_ROOT, 'tools');

/** agent-runtime/config/（runtime.json、models.json；由并行代理维护）。 */
export const CONFIG_DIR = path.join(AGENT_RUNTIME_ROOT, 'config');

/** 截屏等媒体产物目录（与 screen-capture.py 的输出目录一致）。 */
export const MEDIA_DIR = path.join(REPO_ROOT, 'data', 'agent-runtime', 'media');

/** Python 解释器：环境变量优先，缺省用仓库 .venv（与旧 ps-tools 约定一致）。 */
export function resolvePython(): string {
  const fromEnv = process.env.GUANJIA_PYTHON;
  if (fromEnv && fromEnv.trim()) return fromEnv;
  return path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe');
}
