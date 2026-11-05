# 移除 OpenClaw · Agent Runtime 重构 · 进度交接文档

> 2026-11-05 · 给任何新会话/新实例接手用。先读这份,再读总计划。
> 主人指示:**今天完成全部阶段,无过渡态,直接成品。**
>
> **状态更新（2026-11-05 晚）：全部剩余阶段已完成并切换上线，验收数据见
> `docs/plans/2026-11-05-agent-runtime-phase7-report.md`。本档 §4 的 1-5 逐项：**
> 1. Wave1-C 完成（worker 四件套 + 适配器反转 + 双端测试 69+13 全绿）；
> 2. 接线完成（app.ts registerWorkerRoutes、main-agent 真实 registry + toolsFactory 注入、
>    TaskWorkerExecutor GUANJIA_WORKER_BACKEND=ts 分支 + 13 项测试、task_start/status/cancel
>    三能力经 bridge tools_impl→task-ipc）；
> 3. Phase 4 记忆完成（bridge /internal/memory/search 多词打分检索 + staging/review/commit
>    导入 36 条 + Main Agent 每轮检索注入）；
> 4. Phase 5/6 切换完成（BRAIN_URL 默认 18800、approval_ipc 默认 bridge Bearer、
>    voice.env WORKER_BACKEND=ts、语音已重启走新链、OpenClaw 网关进程已停——
>    计划任务 DISABLE 需管理员，命令见报告 §7）；
> 5. Phase 7 报告落盘。

## 0. 权威文档索引

| 文档 | 内容 |
|---|---|
| `.trae/documents/openclaw-removal-agent-runtime-refactor.md` | 总计划:现状审计、技术验证、目标架构、Phase 0-7 |
| `.trae/documents/agents-md-decomposition.md` | AGENTS.md 53 条规则拆解 + D1-D6 决议(§4,主人已拍板) |

## 1. 架构一句话

TS agent-runtime(端口 18800)= 大脑(Main Agent + Worker LangGraph + Tool Registry 门面);Python guanjia-core = 业务真相(权限/任务/记忆/工具执行,桥接服务 18801);语音管道不动;OpenClaw 退役。

## 2. 已完成(全部测试可验证)

### Phase 0(全绿)
- `agent-runtime/` 脚手架:langgraph 1.7.0、@langchain/openai 1.8.0、zod4、fastify;better-sqlite3 Windows 预编译
- 双端契约:`agent-runtime/src/contracts/`(Zod 镜像 Python dataclass)+ 仓库根 `contracts/fixtures/`(7正例+1负例)+ TS 18测 + `tests/test_agent_runtime_contracts.py` 8测
- LangGraph Spike:`test/spike/` 3测——**interrupt 所在节点更新不提交,恢复时节点重跑、interrupt() 返回 resume 值**;SqliteSaver 重启恢复验证通过
- Prompt Compiler:`src/prompts/compiler.ts`(compileRole/assertBudget/分节)+ `prompts/main.md、worker.md`
- `scripts/run-tests.ps1` 已接 TS 层:`-Pattern agent_runtime` 同跑双端

### Wave1-A:Main Agent + 语音入口(完成,全绿)
- `src/config.ts`(runtime.json/models.json,已 gitignore,example 入库)、`src/models/provider.ts`(getChatModel(role))
- `src/main-agent/`:conversation.ts(会话 jsonl 持久化+pendingApproval)、agent.ts(handleVoiceTurn:流式+≤3轮工具+确认词自动重试)、tool-mock.ts(**真实 registry 已就绪,只差一行 import 切换:`./tool-mock.js` → `../tools/registry.js`**)
- `src/server/app.ts`:POST /v1/chat/completions(OpenAI 兼容 SSE,读 x-openclaw-session-key)、/internal/diag/prompt-report、/healthz;Bearer 鉴权
- `prompts/main.md` 成品化(D1-D6 落实,总预算 4500 字符)
- `src/guanjia/brain.py`:加 `GUANJIA_BRAIN_URL` 环境开关(**默认仍指 OpenClaw 18789,行为不变**)
- 真实端到端 SSE 已通(智谱 glm-5.3-flash);**硅基流动 402 欠费**——main 模型暂时只能用智谱,需主人充值或确认换默认

### Wave1-B:Tool Registry + Python 桥接(完成,全绿)
- `src/tools/`:registry.ts(**契约签名**:toLangChainTools(filter,deps)、PolicyConfirmationError{capabilityId,todoId,summary})、capabilities.ts(launch_app/get_weather/get_location/speak/web_fetch/screen_capture/**screen_understand**=截屏+VLM,mainVisible/workerVisible/分级)、policy-client.ts(fail-closed)
- 工具脚本迁移:`agent-runtime/tools/`(launch-app.ps1/apps.json/screen-capture.py/get-location.ps1)
- `src/guanjia/bridge_service.py` + `src/guanjia/bridge/`(端口 18801):policy/check→confirm(一次性凭证120s)→tools/<id>/exec→audit;审批兼容口 /latest /decide(worker 审批登记 /internal/bridge/worker_approval + 回调 resume);speak 走 18795/say;get_weather=open-meteo+定位,北京兜底
- `approval_ipc.py` 加 `GUANJIA_APPROVAL_URL`/`GUANJIA_APPROVAL_CAP`(默认不变)
- **真 VLM 端到端已跑通**:screen_understand 全链路(权限→截屏→Qwen/GLM 视觉→描述屏幕内容)
- `tests/test_bridge_service.py` 73 测全绿

### 环境修复
- `.venv` editable 安装原指向旧仓库 `F:\coding\guanjia\src`,已改指本仓库(`.pth` 已修,import 验证正确)

## 3. 当前测试基线

- `cd agent-runtime && npm test`:61 测全绿(28 基线 + registry 13 + A 的 20);`npm run build` 零错误
- Python:`tests/test_agent_runtime_contracts.py`(8)、`test_bridge_service.py`(73)、`test_permission_*`、`test_p2_*` 全绿
- 主人工作区有**未提交的在途改动**(vision_loop.py/camera-tool.py/AGENTS.md,摄像头误检治理 11-03/04)——不是重构产生,勿动勿混提交

## 4. 剩余任务(按序)

1. **Wave1-C(进行中,被中断)**:TS Worker Runtime(LangGraph 图:plan→act→interrupt审批→verify→outcome;`src/worker/` + runs.ts + routes.ts 插件)+ Python 适配器反转(`src/guanjia/core/agent_runtime_adapter.py` + `GUANJIA_WORKER_BACKEND=openclaw|ts` 开关,默认不变)。规格已在派发 prompt 里;恢复 ID `1503ee27-ca1d-4874-be49-5d5271c7e74c`
2. **接线**:src/server/app.ts 加 `registerWorkerRoutes(app)` 一行;main-agent 真实工具 import 切换(一行)
3. **Phase 4 记忆**:bridge 加 /internal/memory/search(memory/ 模块);OpenClaw workspace 记忆(USER/MEMORY/笔记)经 staging/review/commit 导入
4. **Phase 5/6 切换**:GUANJIA_BRAIN_URL 默认改 18800;GUANJIA_WORKER_BACKEND=ts;重启语音(schtasks /End GuanjiaVoice → 等10秒 → /Run);OpenClaw 网关计划任务停用(保留安装≥30天)
5. **Phase 7 报告**:token 对比(prompt-report)、首字延迟、任务成功率

## 5. 关键接口契约(不可偏离)

```ts
// src/tools/registry.ts(B 已实现,A/C 依赖)
export class PolicyConfirmationError extends Error { capabilityId: string; todoId: string; summary: string }
export function isPolicyConfirmationError(e: unknown): e is PolicyConfirmationError
export function toLangChainTools(filter: {mainVisible?: boolean; workerVisible?: boolean; ids?: string[]}, deps: {bridgeUrl: string; token: string; session_id?: string; credential?: string}): Array<Record<string, unknown>>
// src/models/provider.ts(A 已实现)
export function getChatModel(role: string): BaseChatModel
```
bridge HTTP:POST /internal/policy/check {session_id,capability_id,args,intent?} → {outcome,reason,todo_id?};POST /internal/policy/confirm {todo_id,decision} → {credential}|{denied:true};POST /internal/tools/<id>/exec {args,session_id,credential?} → {ok,output,error?};POST /internal/audit {event}。Bearer 鉴权。

## 6. 悬而未决(需主人)

- 硅基流动 402:充值(保 DeepSeek-V4-Flash 主模型)或确认主模型改用智谱 glm-5.3-flashx(免费)
- 面板 prompt 抽屉编辑的是 OpenClaw workspace 文件,退役后变成惰性(面板其他功能不受影响)——是否要接新 prompt 源,主人定
- 全部完成后是否 git commit(建议与主人 vision 在途改动分开提交)
