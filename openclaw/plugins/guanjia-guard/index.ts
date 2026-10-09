import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createStore } from "./src/store";
import { createGuard } from "./src/guard";
import { startRedactProxy } from "./src/redact";
import { onUserTurn as habitOnUserTurn } from "./src/habits";
import type { LevelsFile } from "./src/levels";

/**
 * 管家 S3 插件入口。三件事：
 *   1. before_tool_call  → 分级中间层（guard.ts）
 *   2. 网关服务          → 上云脱敏代理（redact.ts）
 *   3. before_prompt_build → L2 口头确认 + 习惯草案发问（guard.ts / habits.ts）
 * 所有状态落在 workspace/guanjia/，审计写 workspace/guanjia/audit.jsonl。
 */
const pluginDir = path.dirname(fileURLToPath(import.meta.url));
const defaultsDir = path.join(pluginDir, "defaults");

export default definePluginEntry({
  id: "guanjia-guard",
  name: "Guanjia Guard",
  description: "L0-L3 tool gating, cloud redaction proxy, habit confirmation flow.",
  register(api: any) {
    const cfg = (api.pluginConfig ?? {}) as {
      workspaceDir?: string;
      proxy?: { enabled?: boolean; port?: number; upstreamOrigin?: string };
      habitTz?: string;
    };
    const workspaceDir = cfg.workspaceDir ?? path.join(os.homedir(), ".openclaw", "workspace");
    const store = createStore(workspaceDir, defaultsDir);
    const defaults = JSON.parse(fs.readFileSync(path.join(defaultsDir, "levels.json"), "utf8")) as LevelsFile;
    const guard = createGuard(store, defaults);

    /** 网关服务启动时拿到的调度器 getter；用户确认习惯草案时用它建 cron */
    let getCron: (() => any) | undefined;

    // 1) 工具调用分级
    api.on("before_tool_call", (event: any, ctx: any) => guard.beforeToolCall(event, ctx), { priority: 100 });

    // 3) 用户开口：登记触发来源 → L2 确认优先 → 否则看要不要问习惯草案
    const handledMessages = new Set<string>();
    api.on("before_prompt_build", async (event: any, ctx: any) => {
      guard.noteRun(ctx?.runId, ctx?.trigger);
      if (ctx?.trigger && ctx.trigger !== "user") return;
      if (guard.isProactive(ctx?.runId, ctx?.sessionKey)) return;
      // 同一条用户消息可能因重试多次构建 prompt，只处理一次
      const msgId = event.currentUserMessageId ?? `${ctx?.runId}`;
      if (handledMessages.has(msgId)) return;
      handledMessages.add(msgId);
      if (handledMessages.size > 500) handledMessages.delete(handledMessages.values().next().value!);

      const userText: string = event.currentUserMessage ?? event.prompt ?? "";
      if (!userText.trim()) return;
      const confirmNote = guard.onUserTurn(ctx?.sessionKey, userText);
      if (confirmNote) return { appendContext: confirmNote };
      const cron = getCron?.();
      const habitNote = await habitOnUserTurn(store, userText, cron ? (input) => cron.add(input) : undefined, cfg.habitTz ?? "Asia/Shanghai");
      if (habitNote) return { appendContext: habitNote };
    });

    // 2) 脱敏代理随网关起停
    let stopProxy: (() => Promise<void>) | undefined;
    api.registerService({
      id: "guanjia-guard-service",
      start(ctx: any) {
        getCron = ctx.getCron;
        const p = cfg.proxy ?? {};
        if (p.enabled === false) return;
        stopProxy = startRedactProxy({
          port: p.port ?? 18797,
          upstreamOrigin: p.upstreamOrigin ?? "https://api.siliconflow.cn",
          store,
          logger: api.logger,
        });
      },
      async stop() {
        getCron = undefined;
        await stopProxy?.();
        stopProxy = undefined;
      },
    });
  },
});
