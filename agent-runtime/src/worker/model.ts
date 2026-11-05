/**
 * Worker 角色模型（非流式）。
 *
 * 与 models/provider.ts 的 getChatModel 差异仅在 streaming:false：
 * Worker 图的 plan 节点整段消费模型回复，不需要增量流；
 * 更关键的是智谱网关（18793）的流式 tool_call 增量与 @langchain/openai 的
 * 聚合解析不兼容（tool_calls 丢失、续轮 400「角色信息不能为空」），
 * 非流式下 tool_calls 完整可用（2026-11-05 冒烟实测）。
 */
import type { BaseChatModel } from '@langchain/core/language_models/chat_models';
import { ChatOpenAI } from '@langchain/openai';
import { loadModelConfig } from '../config.js';

export function getWorkerChatModel(): BaseChatModel {
  const cfg = loadModelConfig().roles.worker;
  return new ChatOpenAI({
    model: cfg.model,
    apiKey: cfg.apiKey,
    configuration: { baseURL: cfg.baseUrl },
    streaming: false,
    streamUsage: true,
  });
}
