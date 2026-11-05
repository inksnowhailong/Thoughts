/**
 * Tool Registry 的能力声明契约（Wave1-B）。
 *
 * 能力声明是唯一来源：注册表据此自动生成 LangChain tool schema、
 * 权限元数据与审计字段（对应重构计划 §4.3，消灭五处同步）。
 * 分级（riskLevel）必须与 Python 侧 src/guanjia/bridge/capability_levels.py
 * 及迁移来源 openclaw/plugins/guanjia-guard/defaults/levels.json 保持一致。
 */
import type { z } from 'zod';

export type LatencyClass = 'instant' | 'job';
export type SideEffect = 'none' | 'reversible' | 'destructive';
export type RiskLevel = 'L0' | 'L1' | 'L2' | 'L3';

/** 本机进程执行器：child_process 直接跑固定命令（不进 shell）；`{{key}}` 占位符按校验后的参数替换。 */
export interface LocalProcessExecutor {
  kind: 'local-process';
  command: string[];
  cwd?: string;
  /** 超时毫秒数，缺省 20s；超时按执行失败处理。 */
  timeoutMs?: number;
}

/** Python 原生工具执行器：走 bridge 的 /internal/tools/<id>/exec。 */
export interface HttpPythonExecutor {
  kind: 'http-python';
  /** bridge 侧工具实现路径（当前按 capability id 路由，path 留作语义标注）。 */
  path: string;
}

/** 内置 VLM 执行器：先跑 captureCommand 截屏，再用视觉模型看图回答。 */
export interface BuiltinVlmExecutor {
  kind: 'builtin-vlm';
  captureCommand: string[];
  /** 截屏超时毫秒数，缺省 15s。 */
  timeoutMs?: number;
}

export type CapabilityExecutor = LocalProcessExecutor | HttpPythonExecutor | BuiltinVlmExecutor;

/** 注册表里的一个能力。 */
export interface Capability {
  id: string;
  title: string;
  /** 给模型看的工具描述（LangChain tool description 原样使用）。 */
  description: string;
  /** Zod 输入 schema（LangChain tool schema 由此生成）。 */
  // biome-ignore lint: zod 对象的泛型参数跨 zod3/zod4 版本不稳定，运行时以 z.object 为准
  inputSchema: z.ZodObject<any>;
  latencyClass: LatencyClass;
  sideEffect: SideEffect;
  riskLevel: RiskLevel;
  /** Main Agent 是否可见可绑定。 */
  mainVisible: boolean;
  /** Worker Agent 是否可见可绑定。 */
  workerVisible: boolean;
  executor: CapabilityExecutor;
}
