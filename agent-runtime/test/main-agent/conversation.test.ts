import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { AIMessage, HumanMessage, SystemMessage, ToolMessage } from '@langchain/core/messages';
import { ConversationStore, MAX_ROUNDS, type PendingApproval } from '../../src/main-agent/conversation.js';

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'guanjia-sessions-'));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const pending: PendingApproval = {
  capabilityId: 'app.launch',
  todoId: 'todo-1',
  summary: '打开浏览器',
  toolName: 'launch_app',
  args: { app: 'browser' },
};

describe('ConversationStore', () => {
  it('追加消息后，新 store 实例能从 jsonl 重新加载（持久化）', () => {
    const a = new ConversationStore(dir);
    a.appendMessage('s1', new HumanMessage('打开浏览器'));
    a.appendMessage('s1', new AIMessage({ content: '', tool_calls: [{ id: 'c1', name: 'launch_app', args: { app: 'browser' } }] }));
    a.appendMessage('s1', new ToolMessage({ content: '好的', tool_call_id: 'c1' }));
    a.appendMessage('s1', new AIMessage('已经打开了。'));

    const b = new ConversationStore(dir);
    const s = b.getSession('s1');
    expect(s.messages).toHaveLength(4);
    expect(s.messages[0]).toBeInstanceOf(HumanMessage);
    expect(s.messages[1]).toBeInstanceOf(AIMessage);
    expect((s.messages[1] as AIMessage).tool_calls).toEqual([{ id: 'c1', name: 'launch_app', args: { app: 'browser' } }]);
    expect(s.messages[2]).toBeInstanceOf(ToolMessage);
    expect((s.messages[2] as ToolMessage).tool_call_id).toBe('c1');
    expect((s.messages[3] as AIMessage).content).toBe('已经打开了。');
  });

  it('系统消息与 pendingApproval 一起持久化，重启后恢复', () => {
    const a = new ConversationStore(dir);
    a.appendMessage('s1', new HumanMessage('打开浏览器'));
    a.appendMessage('s1', new SystemMessage('用户已口头确认「app.launch」。'));
    a.setPending('s1', pending);

    const b = new ConversationStore(dir);
    const s = b.getSession('s1');
    expect(s.messages[1]).toBeInstanceOf(SystemMessage);
    expect(s.pending).toEqual(pending);

    // 清除也持久化
    b.setPending('s1', null);
    expect(new ConversationStore(dir).getSession('s1').pending).toBeNull();
  });

  it('内存只保留最近 12 轮，超龄轮次直接丢弃', () => {
    const a = new ConversationStore(dir);
    for (let i = 1; i <= MAX_ROUNDS + 3; i++) {
      a.appendMessage('s1', new HumanMessage(`第${i}轮`));
      a.appendMessage('s1', new AIMessage(`第${i}轮回复`));
    }
    const s = a.getSession('s1');
    const humans = s.messages.filter((m) => m instanceof HumanMessage);
    expect(humans).toHaveLength(MAX_ROUNDS);
    expect((humans[0] as HumanMessage).content).toBe(`第${MAX_ROUNDS + 3 - MAX_ROUNDS + 1}轮`);
    expect((humans[humans.length - 1] as HumanMessage).content).toBe(`第${MAX_ROUNDS + 3}轮`);
  });

  it('加载 jsonl 时只取尾部 12 轮（历史文件比内存窗口长）', () => {
    const a = new ConversationStore(dir);
    for (let i = 1; i <= 15; i++) {
      a.appendMessage('s1', new HumanMessage(`第${i}轮`));
      a.appendMessage('s1', new AIMessage(`第${i}轮回复`));
    }
    const b = new ConversationStore(dir);
    const s = b.getSession('s1');
    expect(s.messages.filter((m) => m instanceof HumanMessage)).toHaveLength(MAX_ROUNDS);
    expect((s.messages[0] as HumanMessage).content).toBe('第4轮'); // 第 4 轮起 = 最近 12 轮
  });

  it('不同 session key 互不串扰，key 做文件名安全化（含 Windows 非法字符）', () => {
    const a = new ConversationStore(dir);
    a.appendMessage('agent:main:main', new HumanMessage('主线'));
    a.appendMessage('default', new HumanMessage('默认'));
    expect(a.getSession('agent:main:main').messages).toHaveLength(1);
    expect(a.getSession('default').messages).toHaveLength(1);
    expect((a.getSession('default').messages[0] as HumanMessage).content).toBe('默认');
  });
});
