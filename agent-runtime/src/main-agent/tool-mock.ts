/**
 * toLangChainTools 的本地 mock，签名与并行实现的 src/tools/registry.ts 完全一致：
 *
 *   export function toLangChainTools(
 *     filter: { mainVisible?: boolean; workerVisible?: boolean; ids?: string[] },
 *     deps: { bridgeUrl: string; token: string },
 *   ): Array<{ name: string; description: string; schema: unknown } & Record<string, unknown>>
 *
 * agent.ts 只从本模块 import toLangChainTools 一个符号；真实 registry 就绪后把
 * import 路径换成 ../tools/registry.js 即可，其余代码不动。
 *
 * mock 行为由 mockToolState 控制（测试直接改写）：
 * - reply：工具正常返回的固定文本；
 * - throwConfirmation：为 true 时每次调用抛 PolicyConfirmationError 形状的错误
 *   （capabilityId / todoId / summary，与 registry 契约一致）；
 * - calls：记录全部调用（含 args），供测试断言确认流是否带 credential 重试。
 */
import { z } from 'zod';

export interface PolicyConfirmationErrorShape extends Error {
  capabilityId: string;
  todoId: string;
  summary: string;
}

export interface MockToolCall {
  name: string;
  args: Record<string, unknown>;
}

export const mockToolState = {
  calls: [] as MockToolCall[],
  throwConfirmation: false,
  reply: '好的，已经办好了。',
};

export function resetMockToolState(): void {
  mockToolState.calls = [];
  mockToolState.throwConfirmation = false;
  mockToolState.reply = '好的，已经办好了。';
}

const mockToolSchema = z.object({
  action: z.string().optional(),
  credential: z.string().optional(),
});

export function toLangChainTools(
  _filter: { mainVisible?: boolean; workerVisible?: boolean; ids?: string[] },
  _deps: { bridgeUrl: string; token: string },
): Array<{ name: string; description: string; schema: unknown } & Record<string, unknown>> {
  return [
    {
      name: 'mock_tool',
      description: '测试用假工具：按 mockToolState 返回固定文本，或抛需要用户确认的错误',
      schema: mockToolSchema,
      async invoke(args: unknown): Promise<string> {
        const a = (args ?? {}) as Record<string, unknown>;
        mockToolState.calls.push({ name: 'mock_tool', args: a });
        if (mockToolState.throwConfirmation) {
          const err = new Error('此操作需要用户确认') as PolicyConfirmationErrorShape;
          err.name = 'PolicyConfirmationError';
          err.capabilityId = 'mock_capability';
          err.todoId = 'todo-mock-1';
          err.summary = '打开浏览器';
          throw err;
        }
        return mockToolState.reply;
      },
    },
  ];
}
