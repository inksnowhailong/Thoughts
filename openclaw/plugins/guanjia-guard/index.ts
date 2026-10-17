import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createStore } from "./src/store";
import { createGuard } from "./src/guard";
import { startApprovalIpc } from "./src/approval-ipc";
import { startRedactProxy } from "./src/redact";
import { onUserTurn as habitOnUserTurn } from "./src/habits";
import { createSpeakTool, stripVoiceTag } from "./src/speak";
import { createGetDesktopStatusTool, createGetLocationTool, createGetWeatherTool, createLaunchAppTool } from "./src/ps-tools";
import { bindTaskApproval, decideTaskApproval, createTaskCancelTool, createTaskReportTool, createTaskStartTool, createTaskStatusTool } from "./src/tasks";
import { recordMainWatermark } from "./src/main-watermark";
import { captureWechatInboundIdentity, diagnoseWechatInboundContext } from "./src/wechat-inbound";
import type { LevelsFile } from "./src/levels";

/**
 * 管家插件入口。四件事：
 *   1. before_tool_call    → 分级中间层（guard.ts）：默认直接做，少数先口头问
 *   2. 网关服务            → 上云脱敏代理（redact.ts）
 *   3. before_prompt_build → 口头确认 / 「以后都允许」+ 习惯草案发问（guard.ts / habits.ts）；语音轮的「[语音 …]」标记先剥掉
 *   4. 工具                → speak（念一句）、launch_app（开程序）、get_location（定位）、get_weather（天气）
 * 所有状态落在 workspace/guanjia/，审计写 workspace/guanjia/audit.jsonl。
 */
function isPermissionShadowEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  const raw = env.GUANJIA_PERMISSION_SHADOW_V1;
  if (!raw) return false;
  return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
}

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
      speakUrl?: string;
    };
    const workspaceDir = cfg.workspaceDir ?? path.join(os.homedir(), ".openclaw", "workspace");
    const store = createStore(workspaceDir, defaultsDir);
    store.audit({ kind: "wechat-inbound-identity", decision: "capture-hook-loaded" });
    const defaults = JSON.parse(fs.readFileSync(path.join(defaultsDir, "levels.json"), "utf8")) as LevelsFile;
    const guard = createGuard(
      store,
      defaults,
      (pending) => { void bindTaskApproval(workspaceDir, pending); },
      { permissionShadowEnabled: isPermissionShadowEnabled() },
    );

    /** 网关服务启动时拿到的调度器 getter；用户确认习惯草案时用它建 cron */
    let getCron: (() => any) | undefined;

    // 1) 工具调用分级
    api.on("before_tool_call", (event: any, ctx: any) => guard.beforeToolCall(event, ctx), { priority: 100 });

    // 3) 用户开口：登记触发来源 → 口头确认优先 → 否则看要不要问习惯草案
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

      // HTTP 语音轮走嵌入式运行器，没有 currentUserMessage，prompt 就是「[语音 …] 原话」
      const rawText: string = event.currentUserMessage ?? event.prompt ?? "";
      const userText = stripVoiceTag(rawText);
      if (!userText.trim()) return;
      diagnoseWechatInboundContext(store, event, ctx);
      captureWechatInboundIdentity(store, ctx);
      recordMainWatermark(store, { sessionKey: ctx?.sessionKey, text: rawText, channel: ctx?.channel, accountId: ctx?.accountId });
      const provenanceConfirm = guard.onProvenanceWechatTurn(ctx, rawText);
      if (provenanceConfirm) return { appendContext: provenanceConfirm };
      const wechatConfirm = guard.onOwnerWechatTurn(ctx?.sessionKey, rawText, ctx?.requester);
      if (wechatConfirm) return { appendContext: wechatConfirm };
      const confirmNote = guard.onUserTurn(ctx?.sessionKey, rawText, ctx?.runId);
      if (confirmNote) return { appendContext: confirmNote };
      const cron = getCron?.();
      const habitNote = await habitOnUserTurn(store, userText, cron ? (input) => cron.add(input) : undefined, cfg.habitTz ?? "Asia/Shanghai");
      if (habitNote) return { appendContext: habitNote };
    });

    // 4) 工具（清单 contracts.tools 里逐个声明）
    api.registerTool(createSpeakTool({ url: cfg.speakUrl }));
    api.registerTool(createLaunchAppTool({ workspaceDir }));
    api.registerTool(createGetLocationTool({ workspaceDir }));
    api.registerTool(createGetDesktopStatusTool({ workspaceDir }));
    api.registerTool(createGetWeatherTool({ workspaceDir }));
    api.registerTool(createTaskStartTool({ workspaceDir }));
    api.registerTool(createTaskStatusTool({ workspaceDir }));
    api.registerTool(createTaskCancelTool({ workspaceDir }));
    api.registerTool(createTaskReportTool({ workspaceDir }));

    // 2) 脱敏代理随网关起停
    let stopProxy: (() => Promise<void>) | undefined;
    let stopApprovalIpc: (() => Promise<void>) | undefined;
    api.registerService({
      id: "guanjia-guard-service",
      start(ctx: any) {
        getCron = ctx.getCron;
        stopApprovalIpc = startApprovalIpc(store, guard.approvals, api.logger,
          (record, decision) => { void decideTaskApproval(workspaceDir, { sessionKey: record.sessionKey, decision }); });
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
        await stopApprovalIpc?.();
        stopApprovalIpc = undefined;
        await stopProxy?.();
        stopProxy = undefined;
      },
    });
  },
});
