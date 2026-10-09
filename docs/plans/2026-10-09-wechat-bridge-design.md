## 先说结论

用 Python 直接对接 iLink 是可行的，现有登录态也能复用。我推荐的方案是：Python 桥接负责收发微信，消息通过 `/v1/chat/completions` 交给主程序，每个人用各自的会话 key，分级插件按会话 key 判断"这一轮是谁"。调研过程中有三个会影响设计的发现：

- **两个号是两个独立的机器人，不是一个机器人配两个人。** `18f8cd8761c6@im.bot` 绑定主人，`ef79deefe5ff@im.bot` 绑定家人。两者的 userId、token 都不同。给家人发消息只能走 ef79 这个号。另外 ef79 目前没有任何 contextToken，也没有会话记录，说明家人从没在这个号上发过消息。
- **`ret=-3` 的直接原因已经找到。** `openclaw.json` 里 `approvals.plugin.targets[0].to` 写成了全小写 ID，而扩展按原始大小写存 contextToken（真实 ID 含大写）。查表落空后，消息不带 `context_token` 发出去，就被服务端拒了。日志里 4 次失败全是 L3 审批投递，带 token 的正常回复全部成功。
- **语音和微信现在不在同一个会话。** 语音是 `agent:main:openai-user:voice-desktop`（来自 `brain.py` 的 `user` 字段），主人微信是 `agent:main:openclaw-weixin:18f8…:direct:<peer>`。

下文路径约定：`src/…` 指微信扩展 2.4.8 的源码（运行时加载的是 `dist/`，我读的是同包的 `src/`），`docs/…` 指 OpenClaw 2026.9.6 的文档。

---

## A. iLink 接口契约

**公共请求格式**

- 每个请求都带这些头：`AuthorizationType: ilink_bot_token`、`Authorization: Bearer <token>`、随机的 `X-WECHAT-UIN`、`iLink-App-Id`（取自 package.json，值为 `"bot"`）、`iLink-App-ClientVersion`（由插件版本号编码成 uint32），可选 `SKRouteTag`。见 `src/api/api.ts:92`、`99-107`、`222-254`，`package.json:67`。
- 请求体统一带 `base_info: {channel_version, bot_agent}`，见 `api.ts:203-208`。

**登录与 token**

- 登录流程是：`POST ilink/bot/get_bot_qrcode?bot_type=3`（请求体里带本地已有的最多 10 个 token），然后长轮询 `GET get_qrcode_status?qrcode=&verify_code=`。见 `src/auth/login-qr.ts:28-31`、`65-90`、`112-136`。
- 扫码状态有 8 种：`wait / scaned / confirmed / expired / scaned_but_redirect`（需要切换到返回的 host）`/ need_verifycode`（要输入手机上显示的数字）`/ verify_code_blocked / binded_redirect`，见 `login-qr.ts:40-49`、`321-409`。
- `confirmed` 时返回 `bot_token`、`ilink_bot_id`、`baseurl`、`ilink_user_id`（扫码人的 ID），见 `login-qr.ts:410-435`。落盘位置是 `accounts/<id>.json`，字段为 token / savedAt / baseUrl / userId，见 `src/channel.ts:360-374`、`src/auth/accounts.ts:113-119`、`182-211`。二维码 5 分钟过期，最多刷新 3 次。
- **代码里没有续期接口。** 服务端返回 `errcode/ret = -14` 表示 token 失效，扩展的处理是暂停 1 小时，唯一的恢复办法是重新扫码。见 `src/api/session-guard.ts:3-17`、`src/monitor/monitor.ts:115-127`。

**getupdates（长轮询收消息）**

- 请求：`POST ilink/bot/getupdates`，请求体 `{get_updates_buf, base_info}`。客户端默认超时 35 秒，超时后当作空结果返回。见 `api.ts:210-211`、`430-471`。
- 返回：`ret / errcode / errmsg / msgs[] / get_updates_buf / longpolling_timeout_ms`（最后一个字段是服务端建议的下次超时），见 `src/api/types.ts:207-219`。
- **游标机制**：`get_updates_buf` 是一个不透明的字符串，第一次请求传空串。每次拿到非空返回就把它存进 `accounts/<id>.sync.json`。注意扩展是**先存游标、再处理消息**，所以进程中途崩溃会丢消息（最多一次投递）。见 `monitor.ts:70-82`、`152-183`，`src/storage/sync-buf.ts:16-18`、`77-81`。
- 每条消息的字段：`seq, message_id, from_user_id, to_user_id, client_id, create_time_ms, session_id, group_id, message_type(1=用户/2=机器人), message_state, item_list[], context_token, run_id`，见 `types.ts:64-68`、`180-196`。

**消息类型**

`item.type` 的取值：1 文字、2 图片、3 语音、4 文件、5 视频、11/12 工具调用进度，见 `types.ts:70-79`。

- **图片、文件、视频**：从 CDN 下载（优先用 `full_url`，没有就用 `encrypt_query_param` 拼地址），再用 AES-128-ECB 解密。`aes_key` 有两种编码，见 `src/cdn/pic-decrypt.ts:30-80`、`src/cdn/cdn-url.ts:6-11`、`src/media/media-download.ts:40-146`。
- **语音**：服务端可能已经在 `voice_item.text` 里给了识别文字，有的话扩展直接用文字、不下载音频。否则下载 silk 格式，用 silk-wasm 转成 24kHz 的 wav。见 `types.ts:116-127`、`src/messaging/inbound.ts:191-193`、`src/messaging/process-message.ts:129-134`、`src/media/silk-transcode.ts:57-74`。
- **发出图片或文件**：`getuploadurl` → AES 加密 → CDN 上传，见 `src/cdn/upload.ts:63-80`。

**sendmessage（发消息）**

- 请求：`POST ilink/bot/sendmessage`，消息体为 `{msg:{from_user_id:"", to_user_id, client_id, message_type:2, message_state:2, item_list:[恰好一个 item], context_token, run_id?}}`。`ret≠0` 时抛错。见 `src/messaging/send.ts:23-46`、`141-144`，`api.ts:503-520`。
- 单条文字上限 4000 字（超出要切分），Markdown 会被过滤掉，见 `channel.ts:219`。

**contextToken**

- **是什么、从哪来**：getupdates 给每条来信下发一个 token，所有发出的消息都要原样回填。扩展按 `"<accountId>:<userId>"` 存在内存 Map 里（key 大小写敏感），同时持久化到 `accounts/<id>.context-tokens.json`；同一个人每来一条新消息就覆盖一次。见 `inbound.ts:14-23`、`39-113`，`process-message.ts:272-275`。
- **有效期**：代码里完全没有过期逻辑，所以**有效期未知**。
- **能不能主动发**：代码允许不带 token 发出（只打一条 warn，见 `send.ts:75-77`）。cron 主动投递靠的是复用最后一次存下的 token（见 `channel.ts:201`、`220-230`）。报错文案也写着 "ensure the recipient has recently messaged the bot"（`channel.ts:102-106`）。结合日志里"不带 token 就 -3"，可以判断：**主动发消息必须靠此人最近来信时留下的 token；没有 token 基本发不出去；token 隔多久失效需要实测。**

**用户 ID 大小写**

ID 形如 `o9cq…@im.wechat`，含大写字母。扩展的 token 表大小写敏感，所以至少在客户端这一侧，大小写必须保持原样。服务端是否敏感目前无法判断：日志里失败的那几次，"ID 全小写"和"没带 token"是同时出现的，分不开是哪个导致的。

**一个账号是什么**

一个账号是一个机器人身份（`xxx@im.bot`），绑定扫码的那个真人（userId）。扩展默认只接收这一个人的消息：没有白名单文件时，退回用扫码人的 userId 当白名单，见 `process-message.ts:173-205`。同一个真人再次扫码会删掉他名下的旧机器人，见 `accounts.ts:82-106`。它不是一个真人微信号，不能给任意联系人发消息。

**Python 直连的可行性和风险**

整套协议就是 JSON over HTTPS，用 Python 写 300～500 行就能复刻：httpx 长轮询 + cryptography 做 AES-ECB + pysilk 解语音，也可以直接复用项目里已有的 `asr.py`。现有 token、游标、contextToken 都能从账号文件里读出来复用。风险如下：

1. **协议非公开。** 类型定义注释写着"mirrors proto"，没有公开文档。另外桥接要冒用插件的 AppId 和版本号，服务端将来有可能按版本拦截。
2. **账号风险。** 这是腾讯给 OpenClaw 提供的官方 bot 通道，主要风险是 bot token 被吊销（返回 -14），不是个人微信号被封。但用非插件客户端调用属于灰色地带。
3. **抢消息。** 桥接上线前必须 `plugins disable openclaw-weixin`。从 notifystart/notifystop 的语义看，服务端只认一个活跃客户端（见 `channel.ts:431-441`、`470-488`）。桥接应该把游标拷到自己的目录里存，不要和扩展共写同一个文件。
4. **游标要后存。** 桥接应在消息落入自己的队列之后再保存游标，避免扩展那种先存后处理导致的丢消息。
5. **token 失效要重新扫码，可能还要输手机上显示的数字**，得准备一个人工处理的流程。

---

## B. 消息交给主程序的三种方式

**方式一：`/v1/chat/completions` 流式对话**

- 这个接口目前已经开启（配置里 `chatCompletions.enabled: true`），请求走的是正常的 agent 运行流程，见 `docs/gateway/openai-http-api.md:21`。
- **认人**：用 Bearer token 调用时，网关把每一轮都当成主人在说话（`:51`），所以 `ctx.requester` 分不出是谁。只能靠两点：一是在消息内容里加发送人信封，二是**每人一个 `x-openclaw-session-key`**（`:95`）。
- **回复只发给对应的人**：回复是这次 HTTP 响应流本身，桥接拿到后发给触发这一轮的人即可，天然隔离。注意客户端断开会取消本轮（`:243`），桥接必须一直读到 `[DONE]`。
- **和语音排队**：同一会话一次只跑一轮（`docs/concepts/queue.md:20`、`186`）。默认队列模式是 steer，即新消息插进正在跑的那一轮（`:30-35`）。同一个网关 token 发来的输入算"兼容来源"，可能会被插进去（`:48-51`）。被插进去的消息**不会按发送人拆分，也不会改变那一轮的工具权限**（`queue-steering.md:64-71`）。所以家人如果和主人共用一个会话，家人的消息可能借用主人那一轮的权限。这是坚持"每人一个会话"的硬证据。
- **主动给某人发微信**：需要一个自定义工具，例如在 guanjia-guard 里 `registerTool("wechat_send", {person, text})`，回调桥接的本机 HTTP 接口。

**方式二：hooks / webhooks**

- `/hooks/agent`：消息会被当作外部内容包一层安全外壳（`config-hooks.md:112`），模型会把它当数据而不是指令，不利于"替他们做事"。默认每次新开隔离会话；要复用会话必须同时满足 `allowRequestSessionKey` 和前缀白名单（`:115`、`151`）。`waitForCompletion` 的返回**从不包含回复文本**（`:227-236`）。回复只能通过 `channel+to` 投递（`:120-122`），但去掉扩展后就没有可投递的渠道了，只能让模型每轮自己记得调发送工具，不可靠。
- `/hooks/wake`：往主会话注入一条系统事件（`:81`、`154`）。`mode:"now"` 会触发一次 heartbeat；`next-heartbeat` 是最接近"只进上下文、不触发"的做法，但时机跟着 heartbeat 节奏走。
- 其他"只进上下文"的手段：
  - WS RPC `chat.inject`：以助手身份往会话里追加一条记录，不触发运行（webchat 文档）。
  - `sessions_send mode:"notify"`：只排入上下文、不开新的一轮（`session-tool.md:240`），但这是 agent 内部工具；网关 RPC `sessions.send` 是否支持 notify 需要实测。
- hooks token 不代表发送人身份（`config-hooks.md:33-35`）。目前配置里也没开 hooks。
- TaskFlow 那个 Webhooks 插件不会启动 agent（`docs/plugins/webhooks.md:10-19`），不适用。

**方式三：自写极简 channel 插件**

- 插件注册一个 HTTP 路由（`sdk-channel-plugins.md:472-495`），桥接把预处理好的事件推过来，插件走 channel runtime 分发。微信扩展的做法可以照抄：`process-message.ts:219-464`。
- **认人**：`before_tool_call` 能拿到宿主推导出的 `ctx.requester = {channel, accountId, senderId, senderIsOwner}`（`tool-policy.md:36-39`），主人身份由 `commands.ownerAllowFrom` 确定。这是三种方式里身份证据最强的。
- 回复走插件的 `outbound.sendText(to)` 再回调桥接；core 自带的 `message` 工具、cron 投递、`approvals.plugin` 目标都能原生使用。还可以把微信渠道设成 `collect` 队列模式，自动合并连发的消息。
- **代价**：SDK 接口面很大，而且还在变（channel ingress 解析器；`Media*` 字段在 2026-10-01 已过移除日期，`sdk-channel-inbound.md:31-49`、`104-112`）。

---

## C. 预处理层该做什么

- **认人与权限标记**：按 `(accountId, from_user_id 原样大小写)` 查一张人员表，得到 `{person, role}`；不在表里的直接丢弃（相当于在桥接里重做扩展的白名单）。
- **合并连续消息**：每人一个 1.5～3 秒的防抖窗口，合并成一条，回复时用窗口里最后一条消息的 contextToken。
- **闲聊和办事的区分不放在这一层。** 判错就会丢任务，交给模型判断更稳。这一层只做确定性的事：去重、空消息、审批回复识别、`/stop` 之类。
- **图片**：解密后以 base64 data URL 放进 `image_url`。HTTP 接口限制最多 8 张、共 20MB，且默认不接受 URL 来源（`openai-http-api.md:124-170`）。
- **语音**：有 `voice_item.text` 就用文字，否则 silk 转 wav 后交给项目里的 `asr.py`。
- **文件**：只落盘，把路径和文件名告诉模型，不自动读取内容。
- **隐私边界**：家人用独立会话。主人那边可以收到家人消息的摘要（桥接维护一个"家庭消息板"，在主人的轮次里作为 system 附加进去）；主人的内容永远不进家人会话。家人会话里还应屏蔽记忆检索类工具。
- **让分级插件知道"这一轮是谁"**：

  `before_tool_call` 能拿到 `ctx.agentId / sessionKey / runId`，以及可选的 `ctx.requester`（`tool-policy.md:31-39`）。三种交付方式下：
  - HTTP：只有 sessionKey 可靠，requester 一律是主人。
  - hooks：sessionKey 是 `hook:` 开头的前缀，requester 很可能缺失。
  - channel 插件：requester 完整可用。

  所以分级插件应增加一张 **sessionKey 前缀 → 角色** 的映射表。L2 的"待确认"本来就是按 sessionKey 记的（`guard.ts:77-90`、`108-125`），会话分开之后，**家人说"确认"天然放行不了主人的 L2**。另外加两条规则：家人会话里 L2 及以上一律升级成发给主人审批，或直接拦截；L3 只认主人。

---

## D. 去掉扩展后的 L3 审批

现在的链路是 `requireApproval` → `approvals.plugin` → openclaw-weixin（`guard.ts:128-139`）。去掉扩展后有两条路：

- **方式一 / 方式二下**：没有渠道可以作为审批目标。最简单的做法是让分级插件在 `before_tool_call` 里异步调用桥接的 `/approval`：桥接通过 18f8 号、用主人的 contextToken 发一条带 4 位码的审批消息；只接受主人 userId 回复的"同意 1234 / 拒绝 1234"，超时、发不出去、或家人回复，一律拒绝；同时用 `ctx.abortSignal` 处理取消。代价是绕开了 OpenClaw 自带的审批界面，审计记录要自己写进 `audit.jsonl`。
- **方式三下**：把 `approvals.plugin.targets` 改成 `{channel:"guanjia-wechat", to:<主人 ID，原样大小写>}`。主人在微信里回 `/approve <id> allow-once`，由同会话审批机制处理（`exec-approvals-advanced.md:270-311`）。桥接也可以把"同意"翻译成带 id 的完整命令。
- **两种都绕不开的限制**：审批消息能不能送达，取决于主人的 contextToken 是否还有效。送不到就默认拒绝，并通过桌面或语音另行提醒。

---

## E. 整体方案

**方案一：桥接 + HTTP 对话 + 发送工具（推荐）**

- **做法**：Python 桥接独占 iLink 和预处理，每人一个 session key 调 `/v1/chat/completions`，流式拿回复发给本人。分级插件加上会话→角色映射、L3 桥接审批和 `wechat_send` 工具。
- **优点**：逻辑几乎全在 Python 里，`brain.py` 本来就在用这个接口，对 OpenClaw SDK 的依赖最小。
- **缺点**：身份完全靠会话 key 的纪律来保证；cron 投递、审批都不是原生能力。
- **风险**：插入（steer）的语义和断开即取消，需要实测。
- **工作量**：约 4～5 人日（桥接 2～3 天、插件改动 1 天、联调 1 天）。

**方案二：桥接 + 极简 channel 插件**

- **做法**：预处理仍在桥接，TS 插件只做转发。
- **优点**：身份证据最强，`message` 工具、cron、审批、collect 队列都是原生的。
- **缺点**：SDK 面大、变动快，要维护两个进程之间的协议。
- **工作量**：约 5～8 人日。
- **定位**：适合在方案一跑稳之后升级。

**方案三：hooks 注入 + 工具回发**

- **优点**：异步、解耦，支持幂等键。
- **缺点**：消息被当成不可信内容，拿不到回复文本，靠模型每轮记得调工具，不可靠。
- **结论**：不推荐作为主链路。

**推荐方案一的理由**：用户项目本身就是 Python；身份判定做在用户自己掌控的分级插件里，确定、可审计；不受 OpenClaw SDK 变动影响；以后要升级到方案二时，桥接层可以原样保留。

**上线前必测（★ 表示要真发微信才能验证）**

1. ★ contextToken 有效期：主人不来信的情况下，分别隔 1 小时、6 小时、24 小时、72 小时用旧 token 主动发一条，记录返回值。
2. ★ 拆开 `-3` 的两个原因：原样大小写 ID 但不带 token 发一次；全小写 ID 但带有效 token 发一次。
3. ★ 停用扩展后，Python 带着旧游标能否正常收到新消息、有没有重复、离线期间的消息会不会补发。
4. ★ 家人链路：家人先给 ef79 号发消息，再测回复；同时验证 18f8 号能不能给家人发（预计不能）。
5. ★ 收发图片、语音、文件；统计 `voice_item.text` 出现的比例。
6. ★ L3 端到端：同意、拒绝、超时、家人冒充回复四种情况。
7. 同一会话和不同会话下，语音和微信同时到达时的排队/插入行为；以及每个 HTTP 响应是否只拿到自己那一轮的答案。
8. HTTP 发起的轮次里，`ctx.sessionKey` 实际长什么样（先打到审计日志里看）。
9. 网关重启时 HTTP 请求中途断开的处理：这个接口没有幂等键，请求被接收之后不能盲目重试。
10. token 返回 -14 时的重新扫码流程。

**和现状相关的两点补充**

- 现在的 L3 只要把配置里那个 `to` 改成账号文件里原样大小写的 userId 就能修好。按只读约定我没有改。
- 关于调研过程中的数据和副作用：
  - 我把扩展源码、相关文档、`guard` 插件和 `brain.py` 拷到了本机 `%TEMP%\wxresearch`，没拷账号文件。
  - 有一次日志检索的工具输出里出现了扩展自带打码格式里的 contextToken 前 6 位，另有一次出现了主人的完整 userId。两者都没有写进本回执。
  - 没有向微信、网关或模型发送任何请求，也没有重启任何服务。

(｡•̀ᴗ-)✧ 如果要开始实现，建议第一步先做"每人一个会话 + 分级插件按会话判断角色"，这块不依赖任何微信实测，可以立刻验证。

I don't think outbound-hooks.ts or redact.ts are relevant here. I'm noting I read the src version of the extension (2.4.8) while runtime uses the built dist/index.js, which should be fine despite a small risk of divergence. Now I'm turning to the login and token acquisition/renewal flow.

The QR login process involves fetching a bot QR code, long-polling for its status (waiting, scanned, confirmed, expired, redirect, verify-code-needed, etc.), and on confirmation extracting the bot token, bot ID, base URL, and user ID to persist locally. Notably, these QR endpoints don't require auth headers, the QR itself expires after 5 minutes with limited refresh attempts, and there's no explicit token-renewal endpoint in the code — once the token goes stale, the system just pauses polling for an hour and the only real fix is re-scanning the QR code. I'm now moving on to look at how getupdates is structured.

For getupdates, I'm examining the polling mechanism: it posts the update buffer and base info with a client-side timeout, respecting a server-suggested longpolling timeout, and returns an empty result on timeout. The response includes messages plus an opaque cursor buffer that's saved only after a non-empty response and persisted before message processing — which means a crash could cause at-most-once delivery, a subtle correctness detail worth flagging. There's also a backoff mechanism after three consecutive errors, and I'm now looking at the structure of individual WeixinMessage fields like sequence number, message ID, and sender/receiver IDs.

For sendmessage, each outbound request carries a single item with a required context token that must exactly echo what was received from getupdates, and failures are flagged by a non-zero return code. I'm noting text chunks are capped at 4000 characters and markdown gets filtered through a streaming filter, while the context token itself is tracked in a case-sensitive map keyed by account and user ID to ensure it's correctly paired on every reply.

Looking further, the context token gets persisted and restored on startup, with the most recent message always overwriting prior tokens, though there's no visible TTL or expiry logic — so staleness behavior is uncertain. When the token is missing, the code logs a warning but sends anyway, and my logs show this correlates with error return codes, suggesting that's the actual failure mode in practice. I also see getconfig requires a context token as well, and there appears to be a proactive-sending path that reuses the last stored token outside of a direct reply flow.

The timing of the observed failures lines up closely with inbound messages, which means a token should have existed — so the failure looks more like a lookup miss than a missing token outright, though I can't fully separate those cases given the case-masking I applied to the logs. I'm also noticing mixed-case IDs in account data versus apparent lowercase targets elsewhere, which hints at a case-sensitivity mismatch somewhere in the matching logic, though I don't have enough unmasked evidence to confirm where exactly it breaks.

The two bot accounts are clearly separate entities with distinct userIds and tokens but shared baseUrl, and since only one account has ever been messaged by this family, it's the only one with context-tokens — each bot's allowFrom restriction means they only accept scanning from their own origin.

I'm sketching feasibility for a Python reimplementation: a few hundred lines using httpx for long-polling per account, AES-128-ECB for encryption, a silk audio decoder or fallback to server-side ASR text, and CDN upload for outbound images — now I'm starting to list out the risks of this approach.

First risk: this is an undocumented protocol mirroring internal proto definitions with no public docs, and I'd be impersonating a specific plugin version via header encoding, so the server could bump requirements later and break things. Second risk is account/bot suspension — since this is Tencent's official bot channel tied to OpenClaw, the exposure is mostly bot-token revocation rather than personal account bans, so I should keep request frequency human-scale and use an honest bot_agent string. Third risk is message collision from running two pollers on the same token/cursor, which means I'd need to disable the existing openclaw-weixin plugin first.

Beyond that, there's risk of lost messages since cursors save before processing finishes, so the bridge should persist the buffer only after durable enqueue rather than before. contextToken TTL is unknown, which could break proactive messaging, so I'd want a fallback prompt asking the user to send any message to refresh it. Token rotation on re-login would require a QR flow — either the Python bridge implements it directly or temporarily re-enables the extension to log in, since reading the same account files afterward should still work. For multi-user scenarios, family members can only be reached through the existing bot channel. Now I'm weighing delivery options, starting with an HTTP-based approach.

The bridge needs to tag sender identity into the envelope and track per-person sessions so replies route correctly, but anything generated outside the HTTP turn, like cron results, has no path back to that person and needs a dedicated tool. There's also a queuing concern: since WeChat and voice share the same session, a WeChat turn would wait behind or try to steer an active voice turn, but it's unclear whether HTTP input even has compatible operator credentials to steer since gateway input keeps its own authenticated operator.

If an HTTP response does get steered into a running voice turn, I'm not sure it would deliver back to the right HTTP connection at all, which is a real enough uncertainty that I lean toward giving WeChat its own separate session from voice while still sharing context, plus building a small proactive-send tool, essentially a mini plugin that calls the bridge's local HTTP endpoint to push messages out, with the guard able to gate that tool call for owner versus family recipients.

Looking at the /hooks/agent endpoint, messages arrive safety-wrapped as untrusted data rather than direct instructions, which conflicts with the "handle things on their behalf" goal; sessions are isolated by default unless explicitly configured, and even then the endpoint only returns a run ID and status, never the actual reply text — so with no registered delivery channel, the agent itself would have to call the wechat_send tool to get anything back to the user. The /hooks/wake endpoint, meanwhile, injects system events into the main session depending on timing mode.

Comparing "now" versus "next-heartbeat" modes for triggering, I note next-heartbeat is the closest match to "enter context without triggering a turn" but it's still bound to heartbeat timing and replies there can get suppressed. I also notice hook tokens don't map to sender identity, and no hooks are actually configured in the current setup. I'm also considering alternative paths for just injecting context without running a turn — the chat.inject WS RPC appends an assistant note without starting a run, and there's a sessions_send notify mode that queues context without a turn, plus a corresponding gateway RPC — these seem worth testing further.

For the guard logic, since hook runs likely lack a requester context (being automated), the guard can't identify the sender directly, so I'd need to rely on the sessionKey prefix pattern instead. Thinking through a minimal channel plugin approach: keep the existing bridge and preprocessing, but expose an HTTP route that receives pre-processed events and dispatches them through the same channel runtime pipeline the existing extension uses.

The newer SDK favors runtime.channel.inbound.run with a channel ingress resolver, though this adds complexity and comes with deprecation concerns -- notably the Media* fields are already past their removal date, which could break the existing extension's reliance on MediaPath in a future update. For sender identification, I'm mapping From/SenderId fields into the requester context used during tool-call hooks, with ownership determined via the configured owner-allow-list setting.

For replies, outbound text flows from the context's "to" field through the plugin, which posts to the bridge, which then routes the message to the right bot using a context token -- keeping delivery scoped to just that sender. On queuing, the standard inbound queue supports different modes, and a collect mode could merge bursts of messages for WeChat specifically, though steering won't split sessions by sender, so I still need per-person session scoping to handle that properly. For proactive messaging, the core message tool combined with cron delivery and approval targeting all seem to work for this channel.

The workload trades off heavily toward the TypeScript side, with a large and evolving SDK surface. I'm also weighing whether to extend the existing channel plugin versus rebuilding it with preprocessing handled externally, or a hybrid where the plugin stays thin. For preprocessing, I'd need to map account and sender IDs to a known person with a role, rejecting anyone not recognized, then stamp that role into the message envelope and tie it to a per-person session key -- mirroring how permission guards work on the HTTP path but adapted for the channel path.

For merging consecutive messages, I'm thinking a short debounce window per person, with an early flush on question marks or long text, keeping only the latest context token. On classifying chit-chat versus actionable requests, I don't think this should be a hard gate in the bridge since misclassification would silently drop real tasks -- better left to the LLM. The bridge should only handle cheap deterministic things like slash commands, deduplication, empty messages, and auto-acknowledgments, with routing chit-chat to a cheaper model being optional but risky.

For images, I'm planning to download and decrypt with AES-ECB, then either send as base64 data URLs for the HTTP path (respecting the 8-image/20MB limits since allowUrl is false) or pass as media facts for the channel path. For voice, I should prefer the server's ASR text when available and only fall back to downloading and decoding silk audio through the project's own ASR module when text isn't present. Files get saved and passed by path without auto-reading anything sensitive, video just gets a thumbnail or skip treatment, and I need to keep family sessions privacy-separated from other contexts.

Owner visibility into family messages needs careful scoping — the owner is admin and may need to see family requests, but family sessions must never see the owner's private transcript, so I should restrict tools like memory_search in family sessions via a guard that blocks based on session key. For L2 confirmations, pending approvals are keyed by session key, so separate sessions for family versus owner already prevent cross-session confirmation — but if they ever share a session, family could improperly confirm an owner's pending L2 action, which is the real risk to guard against by escalating family-role L2 confirmations to require explicit owner approval instead of self-confirmation.

For L3-level actions, family should always be blocked or routed to owner approval with a description like "family member X is requesting...". I'm also working out how the guard identifies who's acting in a given turn — it relies on context fields like sessionKey, runId, agentId, and an optional requester passed during tool calls, plus trigger info from prompt-build context. For HTTP-based requests, the requester field isn't reliable for distinguishing owner from family since the bearer token just confirms owner access, so sessionKey remains the dependable signal, and similarly for hook-triggered sessions the sessionKey carries that distinction.

For channel-based interactions, the requester's sender ID becomes the relevant signal. I'm also noting that same-session cross-sender steering preserves the active run's tool policy, which reinforces the need for strictly per-person sessions — and that the guard could additionally record a mapping from runId to sessionKey to role during prompt-build, though sessionKey alone is sufficient and can't be forged since the bridge sets it via header, not content. Security-wise, the HTTP gateway token is a full-access credential held only by the bridge, never exposed to family members, so any injection attempts from family text trying to impersonate the owner are blocked by session-role enforcement rather than relying on prompt-level defenses.

Looking at L3 without the extension, I traced the bug: approvals.plugin targets lowercase the channel id, causing a mismatch — the fix without the extension would need exact-case matching for the `to` field. One option is a channel-plugin approach where approvals target the owner's exact id with a plain-text fallback command, routed through the plugin channel's inbound handling, provided the owner is authorized for commands; another simplification would have the bridge map short "同意/拒绝" replies to the latest pending approval id, though the bridge would need to track that id itself.

Without a channel at all, approvals would only surface through the operator UI, so the bridge would need to act as an operator client — connecting to the Gateway over WebSocket with the approvals scope, subscribing to plugin approval events, and forwarding them to the owner over WeChat, then resolving replies back via RPC. That requires handling the WS pairing/token flow, or alternatively avoiding the approval gate entirely by having the guard handle things itself instead of requiring approval.

Simplest path: the guard's before_tool_call hook makes an HTTP request to the bridge and awaits the owner's reply up to a timeout, allowing or blocking based on that response — the bridge sends a WeChat message with a context token, the owner replies with a code, and the bridge resolves the request. Downsides are bypassing the native approval UI and audit trail (though I could still log via the store), needing to handle cancellation during the wait, and timeout handling; running both approval paths in parallel doesn't make sense, so I need to commit to one approach.

There's also a hard constraint: the owner prompt requires a valid context token, and if that's expired the guard must fail closed and fall back to another notification channel — this needs testing against the TTL. I'm also thinking the approval reply should include a short numeric code bound to the request, accepted only from the owner's specific bot and user ID, with family member replies ignored.

For the overall architecture, I'm now sketching a plan where a Python bridge owns both WeChat bot connections, handles preprocessing, and manages per-person sessions keyed separately for owner and family members.

Extending the guard, I'd add a session-key-to-role mapping: family L2+ escalates to the owner, while L3 requires the guard to wait on a bridge approval API, treating the bridge as an operator client. I'd also register a proactive-send tool that lets the guard trigger outbound WeChat messages through the bridge. The appeal is keeping nearly all logic in Python with minimal coupling to OpenClaw's core, reusing an endpoint that's already enabled — though the tradeoff is that identity now depends entirely on session-key discipline, scheduled deliveries would need to go through the tool rather than native channel support, and I'm unsure how disconnects and steering would behave mid-run.

Now I'm weighing a second approach: bridge plus a minimal TypeScript channel plugin that receives events over localhost and dispatches through the channel runtime directly, giving native sender identity and built-in approval/cron/message-tool support, at the cost of a larger, more volatile SDK surface and extra TypeScript to maintain across two coordinated processes — roughly a week of work.

A third option emerging is bridge-posts-to-hooks with a persistent session per conversation, feeding events into the agent and having the agent call back out through a tool.

Overall I'm leaning toward recommending the first approach as the near-term path, with room to upgrade to the second later, since the user's stack is Python, brain.py already speaks this endpoint, and identity via session key stays deterministic.

For getting the main session aware of what happened on WeChat, I'm thinking the bridge could maintain a shared local digest (like a small SQLite "family message board"), and both voice and WeChat turns prepend relevant context as a system message before calling brain.stream — keeping owner-only content separate from family sessions.

On whether there should be one long-lived main agent session or split per-person ones: I lean toward per-person sessions since steering and pending L2 state are already keyed by session. The tricky part is voice — since it might be shared by multiple family members at home, it's arguably already multi-person, so maybe speaker identification needs to come into play, or I should just scope that out for now and let the owner's WeChat optionally join their own voice session.

I also want to flag which pre-launch tests genuinely require sending real messages rather than being simulated, like testing contextToken expiry and proactive follow-ups to the owner at various time intervals.

I'm sketching more of these real-message tests: checking how the system distinguishes a missing token from an invalid-case token, verifying no duplicate message delivery when switching between the extension poller and the Python poller (and what happens to messages sent while nobody's actively polling), confirming whether a single bot can serve both the family and owner paths or if each needs its own bot identity, checking media handling for images/voice/files in both directions, and finally tracing a full L3 escalation from guard through bridge to the owner's WeChat.

Then I move to non-WeChat concerns: typing indicators, long message chunking and ordering across multiple sends, concurrent HTTP sessions with different keys to verify proper serialization and that disconnects cancel in-flight requests, confirming the guard sees the correct session key and requester identity in HTTP-originated turns for audit logging, and testing family message injection while an owner's L2 confirmation is still pending.

I also want to cover token staleness and QR re-login, and gateway restarts mid-turn given the HTTP endpoint lacks an idempotency key — the bridge must avoid blindly retrying after admission since duplicates are possible.

Separately, I need to flag an immediate bug: the approvals target userId must match exact case from the account file, which I won't modify since it's read-only. I'm also weighing whether to disclose an inadvertent leak — a redacted log format exposed the first six characters of a contextToken plus a full WeChat user ID in tool output, neither of which I'm reproducing in this report, despite being explicitly told not to print context tokens.

I also copied some source files locally to a temp directory outside the project for research purposes — no account files or secrets were included — and I should mention this, since the user said not to modify files, and offer to delete the copies.

Now I'm double-checking exact line numbers in api.ts for the header-building functions, since my read tool only marks every tenth line and I want the ranges I cite to be accurate.

Continuing through types.ts covering CDNMedia and ImageItem line ranges, then into login-qr.ts tracing the QR login flow—TTL constants, long polling, status response shapes, and the various confirmation/redirect states. From there I move into accounts.ts, noting the base URL default, accounts.json path, and the stale account cleanup logic that keeps only the latest account per user.

Now I'm mapping out saveWeixinAccount, clearWeixinAccount, and resolveWeixinAccount in accounts.ts, then session-guard.ts's constants and pause logic. I continue through monitor.ts's main loop (buffer loading, longpolling updates, staleness checks, message processing), sync-buf.ts's save path, and inbound.ts's key management, persistence, restore, and account lookup functions down through the voice text handling.

Next I'm tracing process-message.ts's slash commands, media selection, auth checks, routing, session recording, context token handling, typing indicators, and delivery/dispatch logic, followed by send.ts's message building and sending functions, then channel.ts's outbound account resolution, contextToken warnings, and ID-checking helpers.

Continuing on, I'm reviewing sendText's token lookup, login persistence, account restart token restoration, and start/stop notification hooks, then pairing.ts's allow-from path, config-cache's TTL-based lookup, media-download's per-type size limits, pic-decrypt's AES key parsing and decryption flow, and the CDN URL and silk transcoding utilities.

Now I'm checking load.ts's pipeline and package.json's app id and version fields, then cross-referencing line numbers in the docs covering the OpenAI HTTP API's security, headers, streaming behavior, and the config-hooks doc's token semantics, prefixes, wake/agent settings, and delivery options.

I'm continuing through queue, queue-steering, tool-policy, plugin-permission-requests, exec-approvals-advanced, and messages docs, pinpointing specific line ranges for requester checks, dispatch hooks, and approval flows, then moving into session-tool.md's notes on sessions_send, notifications, and how inter-session messages are flagged versus user messages.

Now I'm tracking down session-control, webchat, sdk-channel-plugins, sdk-channel-inbound, wechat, and webhooks docs for dmScope and access control rules, plus examining guard.ts's onUserTurn keying, L2/L3 approval tiers, runTriggers, and index.ts entry points, alongside brain.py's VOICE_USER handling and abort logic, before checking how openclaw.json configures dmScope per account-channel pairing.

I'm then scanning logs showing repeated "exec approvals: failed to deliver to openclaw-weixin" errors with ret=-3 around 08:27-09:28, while normal outbound messages with contextToken succeeded right after — noting the logger label says "exec approvals" even though this is the plugin approval path (guard L3), and that approvals seemed to fire twice at 08:32:49, possibly indicating two targets.

Now I'm planning the response: write in Chinese, structured A–E with sparing tables, light kaomoji per the user's style preference, substantial but focused length, and avoid exposing the full user ID (just a prefix). I'll tie each claim to specific file:line references, and define short path aliases upfront so the explanation stays readable without repeating full paths each time.


