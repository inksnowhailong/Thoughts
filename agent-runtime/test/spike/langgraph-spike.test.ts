/**
 * Phase 0 LangGraph Spike —— Worker Agent「审批中断 + 持久化」设计前提的技术验证。
 *
 * 图设计:StateGraph { count: number }
 *   START → nodeA (count+1, 审批门前已完成的工作) → nodeB (受审批保护的动作) → END
 *
 * 关于 interrupt 位置的重要语义(以 @langchain/langgraph@1.7.0 实际行为为准):
 *   LangGraph JS 中 interrupt() 抛出 GraphInterrupt 时,其所在节点的状态更新【不】提交,
 *   恢复时该节点从头重跑、interrupt() 返回 resume 值。
 *   因此本 spike 把 interrupt() 放在受保护的 nodeB 开头,构成「nodeA 完成后的审批门」——
 *   这是官方 human-in-the-loop 模式,恰好验证 Worker 审批场景的关键性质:
 *   审批挂起期间,interrupt 之前各节点(nodeA)的产出已持久化到 checkpoint。
 *
 * 三个测试:
 *   1) interrupt:invoke 后图挂起,结果带 __interrupt__,nodeA 的 count+1 已提交(count==1),nodeB 未执行;
 *   2) resume:同 thread_id 用 Command({ resume }) 恢复,执行 nodeB,count==11;
 *   3) 重启恢复(最重要):全新 SqliteSaver 实例打开同一个 DB 文件 + 全新 graph 实例(模拟进程重启),
 *      同 thread_id invoke(Command({ resume })) 能从 checkpoint 恢复执行,count==11。
 */
import { rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { afterAll, describe, expect, it } from "vitest";
import {
  Annotation,
  Command,
  END,
  INTERRUPT,
  START,
  StateGraph,
  interrupt,
  isInterrupted,
} from "@langchain/langgraph";
import { SqliteSaver } from "@langchain/langgraph-checkpoint-sqlite";

const StateSchema = Annotation.Root({
  count: Annotation<number>(),
});

type GraphState = typeof StateSchema.State;

/** 记录 nodeB 收到的 resume 值,用于断言 interrupt() 的返回值语义。 */
const resumeDecisions: unknown[] = [];

/** 审批门前的工作:count+1。挂起时该节点的产出已提交到 checkpoint。 */
function nodeA(state: GraphState) {
  return { count: state.count + 1 };
}

/** 受审批保护的动作:interrupt() 位于节点开头;恢复时节点重跑,interrupt() 返回 resume 值,再执行 count+10。 */
function nodeB(_state: GraphState) {
  const decision = interrupt("need_approval");
  resumeDecisions.push(decision);
  return { count: _state.count + 10 };
}

function buildGraph(checkpointer: SqliteSaver) {
  return new StateGraph(StateSchema)
    .addNode("nodeA", nodeA)
    .addNode("nodeB", nodeB)
    .addEdge(START, "nodeA")
    .addEdge("nodeA", "nodeB")
    .addEdge("nodeB", END)
    .compile({ checkpointer });
}

/** 收集 saver 连接与 DB 文件,afterAll 统一清理(Windows 下必须先 close 才能删文件)。 */
const savers: SqliteSaver[] = [];
const dbFiles: string[] = [];

function newTempDbPath(): string {
  const p = join(tmpdir(), `guanjia-lg-spike-${randomUUID()}.sqlite3`);
  dbFiles.push(p);
  return p;
}

function newSaver(dbPath: string): SqliteSaver {
  const saver = SqliteSaver.fromConnString(dbPath);
  savers.push(saver);
  return saver;
}

afterAll(() => {
  for (const saver of savers) {
    try {
      saver.db.close();
    } catch {
      // 连接可能已关闭,忽略
    }
  }
  for (const f of dbFiles) {
    try {
      rmSync(f, { force: true });
    } catch {
      // Windows 下文件句柄偶发延迟释放,清理失败不令测试失败
    }
  }
});

describe("LangGraph spike: interrupt + SqliteSaver 持久化 + 断点重启恢复", () => {
  it("测试1 interrupt: invoke 挂起在 nodeA 之后,结果带 __interrupt__,count==1 且 nodeB 未执行", async () => {
    const saver = newSaver(newTempDbPath());
    const graph = buildGraph(saver);
    const threadId = "spike-interrupt";
    const decisionsBefore = resumeDecisions.length;

    const result = await graph.invoke({ count: 0 }, { configurable: { thread_id: threadId } });

    // 结果带 __interrupt__(类型守卫 + 原始保留键双验证)
    expect(isInterrupted(result)).toBe(true);
    const interrupts = (result as Record<string, unknown>)[INTERRUPT] as Array<{
      id?: string;
      value?: unknown;
    }>;
    expect(interrupts).toHaveLength(1);
    expect(interrupts[0]?.value).toBe("need_approval");

    // nodeA 的 count+1 已提交(审批门前的工作已持久化)
    expect((result as GraphState).count).toBe(1);

    // nodeB 未执行:count+10 未生效,且 interrupt() 从未拿到 resume 值
    expect(resumeDecisions.length).toBe(decisionsBefore);
  });

  it("测试2 resume: 同 thread_id 用 Command(resume) 恢复,执行 nodeB,count==11", async () => {
    const saver = newSaver(newTempDbPath());
    const graph = buildGraph(saver);
    const threadId = "spike-resume";

    const suspended = await graph.invoke({ count: 0 }, { configurable: { thread_id: threadId } });
    expect(isInterrupted(suspended)).toBe(true);

    const result = await graph.invoke(new Command({ resume: "approved" }), {
      configurable: { thread_id: threadId },
    });

    // 1(nodeA,已提交) + 10(nodeB,恢复后执行) == 11
    expect((result as GraphState).count).toBe(11);
    // interrupt() 在恢复重跑时返回了 resume 值
    expect(resumeDecisions).toContain("approved");
  });

  it("测试3 重启恢复: 全新 SqliteSaver + 全新 graph 实例打开同一 DB 文件,同 thread_id 恢复执行成功", async () => {
    const dbPath = newTempDbPath();

    // —— 模拟进程 1:跑到审批挂起后"进程死亡"(实例直接丢弃,不 resume、不 close)——
    const saver1 = newSaver(dbPath);
    const graph1 = buildGraph(saver1);
    const threadId = "spike-restart";
    const suspended = await graph1.invoke({ count: 0 }, { configurable: { thread_id: threadId } });
    expect(isInterrupted(suspended)).toBe(true);
    expect((suspended as GraphState).count).toBe(1);

    // —— 模拟进程重启:全新的 SqliteSaver(同一 DB 文件)+ 全新的 graph 实例 ——
    const saver2 = newSaver(dbPath);
    const graph2 = buildGraph(saver2);

    const result = await graph2.invoke(new Command({ resume: "approved-after-restart" }), {
      configurable: { thread_id: threadId },
    });

    // 从 checkpoint 恢复:nodeA 的 +1 来自重启前的持久化,nodeB 在恢复后执行 +10
    expect((result as GraphState).count).toBe(11);
    expect(resumeDecisions).toContain("approved-after-restart");
    // 恢复后的结果不再带 interrupt(图已运行到 END)
    expect(isInterrupted(result)).toBe(false);
  });
});
