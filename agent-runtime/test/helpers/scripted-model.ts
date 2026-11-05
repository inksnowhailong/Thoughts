/**
 * 测试用脚本化聊天模型：按调用顺序依次返回预设响应（可含 tool_calls / usage_metadata），
 * 用于在不访问真实模型 API 的情况下驱动 Main Agent 的工具循环与确认流。
 */
import type { CallbackManagerForLLMRun } from '@langchain/core/callbacks/manager';
import { AIMessage, AIMessageChunk, type BaseMessage } from '@langchain/core/messages';
import { BaseChatModel, type BaseChatModelCallOptions } from '@langchain/core/language_models/chat_models';
import type { BaseLanguageModelInput } from '@langchain/core/language_models/base';
import { ChatGenerationChunk, type ChatResult } from '@langchain/core/outputs';
import type { Runnable } from '@langchain/core/runnables';

export interface ScriptedResponse {
  content?: string;
  /** 多段文本增量（模拟真实模型的逐段流式）；有它时忽略 content 的整段形式 */
  deltas?: string[];
  tool_calls?: Array<{ id: string; name: string; args: Record<string, unknown> }>;
  usage_metadata?: { input_tokens: number; output_tokens: number; total_tokens: number };
}

export class ScriptedChatModel extends BaseChatModel {
  readonly remaining: ScriptedResponse[];

  constructor(script: ScriptedResponse[]) {
    super({});
    this.remaining = [...script];
  }

  _llmType(): string {
    return 'scripted';
  }

  private next(): ScriptedResponse {
    return this.remaining.shift() ?? { content: '（测试脚本已用尽）' };
  }

  async _generate(
    _messages: BaseMessage[],
    _options?: BaseChatModelCallOptions,
    _runManager?: CallbackManagerForLLMRun,
  ): Promise<ChatResult> {
    const r = this.next();
    return {
      generations: [
        { text: r.content ?? '', message: new AIMessage({ content: r.content ?? '', tool_calls: r.tool_calls ?? [] }) },
      ],
    };
  }

  async *_streamResponseChunks(
    _messages: BaseMessage[],
    _options?: BaseChatModelCallOptions,
    _runManager?: CallbackManagerForLLMRun,
  ): AsyncGenerator<ChatGenerationChunk> {
    const r = this.next();
    const extras = {
      ...(r.tool_calls && r.tool_calls.length > 0 ? { tool_calls: r.tool_calls } : {}),
      ...(r.usage_metadata ? { usage_metadata: r.usage_metadata } : {}),
    };
    if (r.deltas && r.deltas.length > 0) {
      // 逐段 yield，最后一段带上 tool_calls / usage_metadata
      for (let i = 0; i < r.deltas.length; i++) {
        yield new ChatGenerationChunk({
          text: r.deltas[i],
          message: new AIMessageChunk(i === r.deltas.length - 1 ? { content: r.deltas[i], ...extras } : { content: r.deltas[i] }),
        });
      }
      return;
    }
    yield new ChatGenerationChunk({
      text: r.content ?? '',
      message: new AIMessageChunk({ content: r.content ?? '', ...extras }),
    });
  }

  bindTools(
    tools: Array<Record<string, unknown>>,
    kwargs?: Partial<BaseChatModelCallOptions>,
  ): Runnable<BaseLanguageModelInput, AIMessageChunk> {
    void tools; // 脚本模型忽略工具声明，行为由脚本决定
    return this.withConfig(kwargs ?? {});
  }
}
