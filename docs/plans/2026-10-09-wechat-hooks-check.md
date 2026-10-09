# 微信接入「钩子路线」只读核实报告（2026-10-09）

范围：OpenClaw 2026.9.6（被控机 fnm v24.21.0 安装）+ @tencent-weixin/openclaw-weixin 2.4.8 + 现有 guanjia-guard。
方式：只读。docs/dist/插件源码拷到本机 `%TEMP%\oc-ro`（dist 为不完整 tar 解出的 10822 个文件，引用到的模块均已确认存在）。未改文件、未重启、未发模型/网关/微信请求、未打印密钥。

路径缩写：
- `docs/` `dist/` = `…/node_modules/openclaw/docs|dist`
- `wx/` = `~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin`
- `cfg` = `C:/Users/inksn/.openclaw/openclaw.json`

---

## 0. 总表

| # | 项目 | 能否（钩子路线） | 机制 | 要实测 | 要改插件 |
|---|---|---|---|---|---|
| 1 | 只并入 18f8 私聊到 agent:main:main | ✅ 能 | `bindings[]` 按 accountId 匹配 + `session.dmScope:"main"` 覆盖；全局保持 `per-account-channel-peer` | 是（路由键、同会话排队） | 否 |
| 2a | 模型看到前改写入站正文 | ❌ 不能改正文；✅ 能加上下文 | 无入站改写钩子；用 `before_prompt_build` / `agent_turn_prepare` 的 `prependContext` 加信封 | 是 | 否（真要改正文才需） |
| 2b | 丢弃 / 认领 | ✅ 能 | `before_dispatch`（claim，可静默或回一句）、`before_agent_run`（block）、`before_agent_reply`（silence） | 是 | 否 |
| 2c | message_received 只读？ | 是只读 | Observe，fire-and-forget | 否 | 否 |
| 2d | before_dispatch 改写/重路由 | ❌ 不能 | 只能 handled（回当前发送人）或放行 | 否 | 否 |
| 2e | 合并连发 | ⚠️ 部分 | `messages.queue.byChannel` 只按渠道；按会话可存 `/queue collect`；插件没接入 inbound debounce | 是 | 按账号去抖需改插件 |
| 3 | 家人摘要静默进主线 | ✅ 能 | **推荐** `api.session.workflow.enqueueNextTurnInjection`（持久、不跑轮、语音轮也会注入）；`enqueueSystemEvent` 不推荐（语音 HTTP 轮不消费、内存、上限 20） | 是 | 否 |
| 4 | 主线主动发微信 | ✅ 能 | `message` 工具 `action:"send", channel:"openclaw-weixin", target:"<精确大小写>@im.wechat", accountId, message`；或插件内 `api.runtime.channel.outbound` | **是（真发）** | 否 |
| 4b | 语音轮不带 target | 不会发到微信 | `agent:main:main` 解析不出投递路由，退回 webchat → 缺 target 报错 | 是 | 否 |
| 4c | contextToken | ⚠️ 有限制 | 插件按「账号:用户ID（精确大小写）」存最近一条入站 token；缺失时照发但带警告；家人号目前无 token 文件 | **是（真发）** | 否 |
| 5a | 审批按发起人分发 | ⚠️ 部分 | `requireApproval` 不能指定收件人；用 `approvals.plugin.mode:"both"`（session=发起会话 + targets=主人） | **是（真发）** | 否 |
| 5b | 家人在微信 /approve | ✅ 现在能；⚠️ 配 ownerAllowFrom 后会失效 | weixin 无审批能力 → 只看 `isAuthorizedSender`；配 owner 后非 owner 降为 reset-only，需同时配 `commands.allowFrom` | **是（真发）** | 否 |
| 5c | 审批目标 ID 大小写 | ✅ 改配置即可 | cfg 里 `to` 全小写，真实 ID 大小写不同；会话键会把 peer 小写化 | 是 | 否 |
| 6 | 钩子里认人 | ✅ 能 | `ctx.requester.{channel,accountId,senderIsOwner}`；`senderId` 永远缺；`ctx.sessionKey`=真实会话键 | 是 | 否（填 SenderId/Name 需改插件，但非必需） |
| 7a | 语音回答不发微信 / 主人微信回微信 | ✅ | HTTP `deliver:false`；微信回复走插件自己的 deliver 回 `ctx.To` | 是 | 否 |
| 7b | lastRoute 被家人覆盖 | ❌ **并入一个号后风险仍在** | 插件对每条入站（含家人）都 `updateLastRoute` 到 `mainSessionKey`，且不传 owner pin | 是 | **要改才能根治**（1 行级） |
| 8a | 主人微信更新 lastInteractionAt | ✅ | channel 轮 `lastInteractionAt=now`；语音 HTTP 轮也 touch | 是 | 否 |
| 8b | 家人摘要/家人入站不更新主线活动 | ✅ | 注入只改 `updatedAt`；家人入站的 lastRoute 写入用 `preserveActivity` | 是 | 否 |
| 8c | reset 后插件继续投递 | ✅（按 key 不按 sessionId） | 但 reset 会**清掉未消费的注入和 system event** | 是 | 否 |
| 9 | 版本风险 | ⚠️ 高 | 插件只检查下限；所依赖的 `channelRuntime.reply/session` 与 `Media*` 字段均在 removeAfter=2026-10-01 的兼容记录里 | — | 升级后可能需要 |

**结论**：所有业务需求都能用「配置 + 我们自己的插件钩子」做到。唯一钩子做不到且影响行为的是 **7b（家人消息覆盖主线 lastRoute）**；只要不依赖 `last` 路由（审批走 turnSource、主动发消息显式带 target），它可以被绕开而非必须改插件。

---

## 1. 只并 18f8 私聊

**配置写法**（全局 `session.dmScope` 保持 `per-account-channel-peer`，cfg:L223 已是）：

```json5
bindings: [
  {
    agentId: "main",
    match: { channel: "openclaw-weixin", accountId: "18f8cd8761c6-im-bot" },
    session: { dmScope: "main" }
  },
  { agentId: "main", match: { channel: "openclaw-weixin", accountId: "*" } }  // 可选，显式兜底
]
```

证据：
- binding 支持 `session.dmScope` 覆盖匹配到的私聊；省略 accountId 只匹配默认账号，所以必须写明：`docs/concepts/agent-bindings.md` L100–110、L117–119；`docs/channels/channel-routing.md` L42–45。
- 源码：`dist/resolve-route-6ukGCxYl.mjs` L328–358（`effectiveDmScope = sessionOverride?.dmScope ?? dmScope`）、L387–391（`choose(..., matched.binding.session)`）。
- 插件确实走 `resolveAgentRoute` 并把 `route.sessionKey` 写回 `ctx.SessionKey`：`wx/dist/src/messaging/process-message.js` L142–159。
- 家人会话键形如 `agent:main:openclaw-weixin:ef79deefe5ff-im-bot:direct:<peer 小写>`：`dist/session-key-CBvmC8zz.mjs` L359–363。

**与语音 HTTP 同会话的冲突**：
- 同一 session lane 串行：`docs/concepts/queue.md` L20、L187。来源不兼容的输入不 steer，按 FIFO 等：同文件 L48–51。主人语音和主人微信在同一时刻碰头时，后到者要等前者跑完（需实测是 steer 还是排队）。
- 工具/沙箱策略键不同：主线里的外部私聊用派生键 `agent:main:openclaw-weixin:18f8…:direct:<peer>` 做策略分类（`dist/runtime-policy-session-key-Dln1Uhpe.mjs` L40–69；`docs/channels/channel-routing.md` L47–49），语音用 `agent:main:main`。若以后启用 `sandbox` 的 non-main 类模式，主人微信轮会被当成非主会话。目前 cfg 无沙箱配置，记为注意点。

## 2. 入站预处理

| 需求 | 结论 | 证据 |
|---|---|---|
| 改写入站正文 | 没有入站改写钩子。`before_dispatch` 明确「是认领，不是改写入/出站内容的 API」 | `docs/plugins/hooks/messages.md` L14–19；钩子目录 `docs/plugins/hooks/reference.md` L155–172 |
| 加发送人信封 | 用 `before_prompt_build`（或 `agent_turn_prepare`）返回 `prependContext`/`appendContext`；仅影响本轮提示，转写里仍是原文 | `docs/plugins/hooks/prompt-and-session.md` L30–57、L67–72 |
| 规范化落库内容 | `before_message_write`（同步）可改写/阻止转写写入；对用户消息是否生效未证实 | `reference.md` L152；需实测 |
| 丢弃 / 认领 | `before_dispatch` 返回 `{handled:true}`（可带 text 回当前发送人）；`before_agent_run` 返回 `{outcome:"block"}` 只存替换文本；`before_agent_reply` 可静默 | `dist/dispatch-from-config-Cu599NRF.mjs` L898–958；`prompt-and-session.md` L149–160；`reference.md` L131 |
| message_received | Observe，返回值忽略，fire-and-forget 发出；字段含 content、from、channelId、accountId、conversationId、messageId、metadata | `reference.md` L22–28、L160；`dist/dispatch-from-config-Cu599NRF.mjs` L2580–2584；`docs/automation/hooks/event-types.md` L116–128 |
| before_dispatch 重路由 | 不能。事件只有 content/body/channel/sessionKey/senderId/isGroup/timestamp，ctx 有 accountId/conversationId；handled 后只回当前会话 | `dispatch-from-config` L910–958 |
| inbound_claim | 只发给拥有该会话绑定的插件，不是全局预路由 | `reference.md` L168–172 |
| 合并连发 | `messages.queue.byChannel: { "openclaw-weixin": "collect" }` 只按渠道，`cap/drop` 只有全局；按会话可用 `/queue collect` 存到会话上（会话行有 `queueMode` 字段）；`messages.inbound` 去抖由渠道插件实现，weixin 插件没用 debounce helper | `docs/concepts/queue.md` L59–71、L110–123；`dist/get-reply-DXzvDRjY.mjs` L5754；`docs/concepts/messages.md` L34–60；`wx/dist/src/**` 无 debounce |

钩子权限：非内置插件用 `before_prompt_build` 等需 `plugins.entries.<id>.hooks.allowConversationAccess: true`（guanjia-guard 已有，cfg:L94）；`allowPromptInjection:false` 会屏蔽 prompt 钩子和 next-turn 注入：`docs/plugins/hooks.md` L125–134。

## 3. 家人摘要静默进主线

| 方式 | 不跑轮 | 语音 HTTP 轮能带入 | 持久 | 上限 | 评价 |
|---|---|---|---|---|---|
| `api.session.workflow.enqueueNextTurnInjection({sessionKey:"agent:main:main", agentId:"main", text, idempotencyKey, placement, ttlMs})` | ✅ | ✅（在 embedded prompt build 里 drain） | ✅ 存在会话行上 | 每插件每会话 32 条、单条 32768 字符，满了直接拒绝不挤掉旧的 | **推荐** |
| `api.runtime.system.enqueueSystemEvent(text,{agentId,sessionKey})` | ✅ | ❌ 只在 channel 回复路径（get-reply）和心跳里 drain，HTTP agent-command 路径不引用 | ❌ 内存 | 每会话 20 条，超出丢最旧 | 不推荐 |
| `chat.inject` | ✅ | — | ✅ | — | 是 assistant 备注，只给 UI 看，不适合当输入 |
| `sessions_send` `mode:"notify"` | ✅ | — | — | — | 是 agent 工具，不是插件 API |
| `/hooks/wake` | ⚠️ 会请求心跳 | — | — | — | 会唤醒，违背「静默」 |

证据：
- next-turn 注入：`docs/plugins/hooks/prompt-and-session.md` L306–345；`docs/plugins/sdk-overview/host-hooks.md` L26；类型 `dist/cli-backend.types-B-oosdMB.d.ts` L36134–36159；实现与上限 `dist/host-hook-state-DEUXoydK.mjs` L14–16、L51–157（L131 满即拒，L141 只改 `updatedAt`）；drain 位置 `dist/context-engine-maintenance-VqmqPz8h.mjs` L338–349（提示构建时，每 run 一次）。
- system event：`docs/plugins/sdk-runtime/state-and-system.md` L38–66；`dist/system-events-ANKIkU0W.mjs` L11、L91；只在 `dist/get-reply-DXzvDRjY.mjs` L3268–3291 和 heartbeat runner 消费；`dist/agent-command-Dyd2gex-.mjs` 不引用 system events。
- 心跳关闭时：next-turn 注入不依赖心跳，随下一轮（语音或主人微信）注入。system event 在心跳关闭时只会随下一条**主人微信**入站带入，语音轮看不到。
- **reset 会清空**未消费的注入和 system event：`prompt-and-session.md` L339–345；`dist/session-reset-cleanup-hKw4VuyJ.mjs` L45–56；`docs/concepts/session.md` L171–176。休眠脚本 reset 前要先把待注入摘要导出，或者摘要存在我们自己的存储里，reset 后再补注入。

**摘要由谁生成**：
- 确定性做法（推荐作为主路径）：在 `message_received` 里按 `ctx.accountId===ef79…` 过滤，取 `from`（精确大小写 ID）映射到家人名字，正文截断 N 字，`idempotencyKey = messageId`。不调模型，入站即入队。
- 语义做法：在 `agent_end`（Observe，可拿到最终 messages）里，从家人支线这一轮的回复里抽取「需要主人决定」的结论，再入队一条，并另发微信通知主人。
- 两者可以并存：每条消息一行确定性摘要，再加 0 到 1 行支线结论。

## 4. 主线主动发微信

**用法**（字段名见 `dist/openclaw-tools-thu-J91q.mjs` L6551–6554、L6660）：
```json
{"action":"send","channel":"openclaw-weixin","target":"<精确大小写>@im.wechat","accountId":"ef79deefe5ff-im-bot","message":"……"}
```
- 显式 accountId 会被校验，并受「可信回合账号」约束：同文件 L8023–8063（`enforceTrustedTurnExplicitAccount`）。主人微信那一轮里用 ef79 账号发给家人是否被拒，**需实测**。
- 插件侧：`sendText` 用 `ctx.accountId`；没给时按 contextToken 反查账号，多账号歧义会报错：`wx/dist/src/channel.js` L44–67、L170–181。
- 插件内也能直接发：`api.runtime.channel.outbound` 可加载渠道的 outbound adapter（`docs/plugins/sdk-runtime/channel.md` L32）。外部插件能否拿到需实测。

**语音轮不带 target**：
- 当前渠道是 webchat 时，会尝试从**会话键**推断投递目标：`openclaw-tools` L6930–6969。`agent:main:main` 解析不出路由（`dist/session-key-CBvmC8zz.mjs` L194–199，冒号分段少于 3 段时返回 null），于是退回 webchat，缺 target 报错，不会悄悄发到微信。
- HTTP 路径默认不强制显式 target（`dist/attempt-execution.runtime-qe49a-CD.mjs` L462），但上面的推断已经兜住了。
- 注意：插件给模型的提示写着「不需要指定 to，自动用当前会话」（`channel.js` L148–152）。这句在微信轮里成立，在语音轮里不成立。

**contextToken 限制**：
- token 来自每条入站消息，按 `accountId:userId`（精确大小写）存储，并持久化到 `accounts/<acct>.context-tokens.json`，启动时恢复：`wx/dist/src/messaging/inbound.js` L11–99；`process-message.js` L179–182；`channel.js` L347。
- 缺 token 时打警告但照发：`channel.js` L77–79；`send.js` L44–45。README 只说「回复时必须回传」，没有写有效期：`wx/README.md` L173–190、L288。
- 现状（只看了文件名和大小写匹配，未读内容）：`accounts/` 下只有 18f8 有 context-tokens 文件，**ef79 没有**。主动发给家人目前会走无 token 发送，能否送达**必须真发验证**；对方很久不说话后 token 是否过期也要验证。

## 5. L3 审批按发起人分发

- `requireApproval` 的字段只有 title/description/scope/severity/timeoutMs/allowedDecisions/onResolution，**不能指定收件人**：`docs/plugins/hooks/tool-policy.md` L44–62；`docs/plugins/plugin-permission-requests.md` L76–89。
- 路由靠 `approvals.plugin.mode`：`"session"`（发起会话）、`"targets"`（静态目标）、`"both"`；另有 `agentFilter`/`sessionFilter`：`docs/tools/exec-approvals-advanced.md` L245–260、L272–296；`dist/schema-RpXu-eUV.mjs` L1032。现配置是 `mode:"targets"`、目标走 18f8：cfg L235–246。
- session 模式取目标的逻辑：`resolveSessionDeliveryTarget(entry, "last", turnSource*)`，**只要 turnSourceChannel 有值就只用 turnSource，否则退回会话里存的 lastRoute**：`dist/exec-approval-session-target-BQ-EIXkd.mjs` L56–78；`dist/targets-session-Ea4onl3I.mjs` L23–83（L32、L40–42）。插件审批请求的 turnSource 取自工具上下文：`dist/agent-tools.before-tool-call-D6M7yTR3.mjs` L989–1009；`dist/agent-tools-g36ho974.mjs` L800–831。

建议配置 `mode:"both"`，targets 只放主人：
- 家人发起：session → 家人自己的支线会话（投递到家人微信），targets → 主人（既是通知，也可以批）。满足「家人批 + 通知主人」。
- 主人微信发起：session 和 targets 都指向主人，可能收到两份（需实测是否去重）。
- 主人语音发起：turnSource 应为 webchat → session 没有可投递目标，只走 targets 到主人微信，或在桌面 Control UI 批。**风险**：如果 HTTP 轮的 turnSourceChannel 是空的，就会退回主线 lastRoute，而 lastRoute 可能已被家人覆盖（见 7b），审批卡会发到家人。**必须实测**。
- 「家人发起只能家人批」做不到：`/approve <id>` 不校验审批人是否属于发起会话，主人拿到 ID 也能批，反之亦然。

**家人在微信 /approve 的权限**：
- `/approve` 不是 owner-only：`docs/tools/slash-commands.md` L326。
- weixin 没注册审批能力，所以授权只看 `isAuthorizedSender`：`dist/commands-approve-C6Mjjz1E.mjs` L17–47、L114–151。插件用「框架 allowFrom 存储，否则回退到扫码者 userId」来判 `CommandAuthorized`：`process-message.js` L104–137。所以扫 ef79 码的家人**现在**是被授权的。同一个账号上的其他家人如果不在 allowFrom 里，消息会被直接丢弃（L133–136）。
- ⚠️ 一旦配置 `commands.ownerAllowFrom`，非 owner 会降为 `reset-only`，`isAuthorizedSender` 变 false，家人的 `/approve` 会被静默忽略：`dist/command-auth-8IGFzIiu.mjs` L340–371（L370）、L395–465。解决办法是同时配置 `commands.allowFrom`（配置后它是命令授权的唯一来源）：`docs/tools/slash-commands.md` L82–85、L139–148、L154–159。

**目标 ID 大小写**：
- cfg L245 的 `to` 是全小写；在 `openclaw-weixin/accounts/` 里**区分大小写搜不到、不区分大小写能搜到**（命中 18f8 的账号文件和 token 文件），说明真实 ID 含大写。插件 token 按精确 ID 取，于是取不到 token。
- 小写的来源很可能是会话键：私聊 peer 在建会话键时被小写化，`dist/session-key-CBvmC8zz.mjs` L359。
- 修法：从 `accounts/18f8cd8761c6-im-bot.json` 的 `userId`（人工查看，注意不要连带泄露 token）或入站日志的 `from=` 抄出原样 ID，写入 `approvals.plugin.targets[].to`、`commands.ownerAllowFrom`、`commands.allowFrom`。代码里任何 `to` 都不能从会话键推导，要用 `message_received.from` / `deliveryContext.to` / `ctx.OriginatingTo`。

## 6. 认人

`ctx.requester` 的构造：`dist/agent-tools-g36ho974.mjs` L800–831；`ctx.sessionKey = runSessionKey ?? sessionKey`（L357）。embedded 路径里 `runSessionKey` 是真实会话键，`sessionKey` 是沙箱/策略键：`dist/builtin-openclaw-q1hiFm14.mjs` L19947–19957。

| 轮次 | ctx.sessionKey | requester.channel | requester.accountId | senderIsOwner（现状 → 配 ownerAllowFrom 后） | senderId |
|---|---|---|---|---|---|
| 主人微信（主线） | `agent:main:main` | `openclaw-weixin` | `18f8cd8761c6-im-bot` | false → true | 缺 |
| 家人支线 | `agent:main:openclaw-weixin:ef79deefe5ff-im-bot:direct:<小写 peer>` | `openclaw-weixin` | `ef79deefe5ff-im-bot` | false | 缺 |
| 语音 HTTP | `agent:main:main` | `webchat`（默认消息渠道） | 无 | true（token 调用按 owner） | 缺 |

以上全部需要实测确认。

- senderId 永远缺，原因是工具上下文的 senderId 取自 `sessionCtx.SenderId`（`dist/get-reply-DXzvDRjY.mjs` L4365），而 `finalizeInboundContext` 不回填（`dist/inbound-context-BLR5MPPS.mjs` 只处理 CommandAuthorized），插件也不填（`wx/…/inbound.js` L162–175）。
- 不配 ownerAllowFrom 时，两个微信号都不是 owner：`command-auth` L435–443（explicitOwners 为空）。配了之后用 `ctx.From`（精确 ID）作为候选也能匹配上（L425–433），所以缺 SenderId 不影响 owner 判定。代价见 5b。
- **账号到人的映射完全可以在我们的钩子里做**：accountId 是可信的（系统提示里有可信的 `account_id`，见上午报告 `inbound-meta` L358–383），家人支线还可以用 `message_received.from` 区分同一账号下的多个人。缺 SenderName 只影响模型自己能否看到名字，可以用 `prependContext` 补一行「发送人：××」。
- guanjia-guard 需要改的地方：待确认 map 的键改成 `sessionKey + requester.accountId`；`requester.channel==="webchat"` 视为语音。

## 7. 共享主线里的回复路由

- 语音：HTTP 固定 `deliver:false`（上午报告 `openai-tool-choice` L240、L243），不会发到微信。
- 主人微信：回复走插件自己传给 `createReplyDispatcherWithTyping` 的 deliver，固定回 `ctx.To` 和本账号：`process-message.js` L227–345，然后 `dispatchReplyFromConfig`（L348–360）。
- **7b：只并入一个号之后，lastRoute 被覆盖的风险并没有消失。** 插件对每条入站（包括家人支线的消息）都调用 `recordInboundSession({updateLastRoute:{sessionKey: route.mainSessionKey, …}})`（`process-message.js` L166–176），而 `mainSessionKey` 恒为 `agent:main:main`（`resolve-route` L342）。core 只有在调用方传 `mainDmOwnerPin` 时才跳过（`dist/session-DSFCkdc0.mjs` L6–16、L33–50），插件没传。
  - 结果：家人每说一句，主线的 deliveryContext/lastRoute 就变成家人。
  - 受影响的场景：approvals session 模式的回退（第 5 节）、`target:"last"` 的心跳/cron、子代理回报复用 lastChannel、`sessions_send` 进 webchat 轮时继承外部路由（上午报告 `effective-reply-route` L125–134）。
  - 不受影响的场景：正常回复、message 工具（不按 lastRoute 推断）。
  - 根治需要改插件：只在 `route.lastRoutePolicy==="main"` 时才写 mainSessionKey，或者传 owner pin。钩子做不到。

## 8. 休眠配合

- 主人微信入站：channel 轮 `lastInteractionAt = now`，system event 轮不更新：`dist/get-reply-DXzvDRjY.mjs` L5742。会话 delivery 也会更新为主人：L5700–5732。
- 语音 HTTP：初次 touch 时 `lastInteractionAt = now`：`dist/agent-command-Dyd2gex-.mjs` L3486–3500。run 期间写的是 restart-recovery 声明，不改 delivery：L3654–3693。
- 家人摘要注入：只改 `updatedAt`：`host-hook-state` L141。
- 家人入站对主线的 lastRoute 写入：`preserveActivity: true`，`dist/session-accessor.sqlite-entry-UCl9kr-O.mjs` L743–776（L773）。
- 结论：家人消息不算主线活动。但休眠脚本要读 `lastInteractionAt`，不能读 `updatedAt`（`docs/concepts/session.md` L230–235）。
- reset 之后：插件按会话键投递（`ctx.SessionKey`），不持有 sessionId，所以 reset 后自动用新的 sessionId。reset 会清空未消费的注入和 system event（见第 3 节）。delivery/lastRoute 在 reset 后是否保留需实测（`clearRotatedSessionMetadata` 只清 recovery/运行字段）。
- 家人支线空闲 4 小时：`session.resetByType.direct` 可能连带作用到 `agent:main:main`（它承接主人私聊），建议也用脚本做，或者先实测主线的 chatType。

## 9. 版本风险

- 插件检查范围：运行时只检查下限 `>=2026.3.22`（`wx/dist/src/compat.js` L9、L38–62），`package.json` 的 `peerDependencies.openclaw` 是 `>=2026.5.12`，**都没有上限**，升级后不会报警：`docs/channels/wechat.md` L108–118。
- 插件依赖的 `channelRuntime.reply.dispatchReplyFromConfig / finalizeInboundContext / createReplyDispatcherWithTyping / resolveHumanDelayConfig`、`channelRuntime.session.resolveStorePath / recordInboundSession` 都在兼容记录 `plugin-runtime-api-compat-aliases` 里，removeAfter 是 **2026-10-01**（已过）：`dist/cli-backend.types-B-oosdMB.d.ts` L26453–26462；`docs/plugins/sdk-runtime/channel.md` L95。
- `MediaPath/MediaType` 属于 `media-legacy-projection`，removeAfter 同样是 2026-10-01，且以「已发布插件清扫干净」为前提：`docs/plugins/sdk-migration/compatibility-policy.md` L326–342。插件入站只传 MediaPath：`inbound.js` L156–194。
- 实际影响：2026.9.6 仍然保留这些别名，现在能用。任何晚于 removeAfter 的版本都可能让**整个微信入站失效**（不只是媒体），而且不会有版本报警。
- 升级策略：**锁版本**。锁定 OpenClaw 2026.9.6 和插件 2.4.8，关闭自动更新。每次升级前在 release notes / 兼容注册表里确认这两条记录的状态。腾讯发布适配新 API 的插件版本后，再一起升级；否则走「拷一份插件改」的路线。

## 10. 汇总

**钩子做不到、需要改插件的**（都不是首期必需）：
1. 家人入站覆盖主线 lastRoute（7b）：改 1 行级。不改的话，全系统禁止依赖 `last` 路由。
2. 入站正文改写 / SenderName 写进 ctx：只能加上下文；真要改需要改插件或 core。
3. 按账号的入站去抖：插件没接 debounce。
4. 升级到移除兼容别名之后的 OpenClaw 版本：需要新版插件或自行 fork。

**钩子路线工作量重估**（不含插件 fork）：
| 块 | 估计 |
|---|---|
| 配置：bindings、ownerAllowFrom + commands.allowFrom、approvals.plugin both + 修 ID 大小写、queue collect | 0.5 天 |
| 插件：message_received → 家人摘要 → enqueueNextTurnInjection（幂等）；agent_end 抽「需主人决定」；通知主人（outbound adapter 或交给模型） | 1–1.5 天 |
| guard：账号到人映射、待确认按 (sessionKey, accountId) 存、语音/主人/家人分级、审批描述带发起人 | 0.5–1 天 |
| 休眠：reset 前导出/补注入摘要、读 lastInteractionAt、家人支线 4h 脚本 | 0.5–1 天 |
| 联调实测（含真发微信） | 1–1.5 天 |
| **合计** | **约 4–5.5 人日** |

**上线前必须实测**（★ = 需要真发微信）：
1. binding 生效：18f8 进 `agent:main:main`，ef79 进按人的键（看插件 debug 日志里的 `resolveAgentRoute`）。
2. ★ 主人微信回复回到主人；语音与主人微信并发时的排队/steer 行为和等待时长。
3. 三种轮次下 `before_tool_call` 的 `ctx.sessionKey` 和 `ctx.requester` 实际值（打审计日志即可，不需要真发）。
4. ★ 家人发消息后，主线 lastRoute 是否变成家人（`openclaw sessions --json` 看 agent:main:main 的 delivery）。
5. `enqueueNextTurnInjection` 在语音轮和主人微信轮都被注入；在主人微信轮里，drain 用的键是否是 `agent:main:main` 而不是派生键。
6. ★ 主动发微信：message 工具带 accountId 发给家人（ef79 没有 token）、发给主人、主人微信轮里跨账号发送是否被 `enforceTrustedTurnExplicitAccount` 拒绝。
7. ★ 审批：语音发起的 L3 是否只到主人（重点验证 turnSource 为空时会不会落到家人）；家人发起是否双发；主人微信发起是否重复。
8. ★ 配置 ownerAllowFrom + commands.allowFrom 后，家人 `/approve` 仍然有效、主人 owner 判定为 true。
9. ★ 修正大小写后的审批目标能带 token 送达。
10. sessions.reset 后：插件继续投递到新 sessionId；delivery 是否保留；未消费的注入被清掉（验证导出/补注入流程）。
11. 家人摘要和家人入站都不会让 `lastInteractionAt` 前移。
12. `api.runtime.channel.outbound` 对外部插件是否可用（不真发，只验证能拿到 adapter）。
