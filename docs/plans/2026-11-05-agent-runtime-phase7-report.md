# OpenClaw 退役 · Agent Runtime 切换 · 验收报告（Phase 7）

> 2026-11-05 · Wave1-C → Phase 5/6 全部完成当日的验收数据。
> 回滚总开关：`GUANJIA_BRAIN_URL=http://127.0.0.1:18789/v1/chat/completions`（env 或 tmp/voice.env），重启语音即回旧链。

## 1. 固定 token（目标 ≥60% 削减）

| 链路 | 固定 prompt | 来源 |
|---|---|---|
| 旧链（OpenClaw + AGENTS.md 大 prompt） | ≈16,667 token | scripts/prompt_size.py（2026-10-15 实测） |
| 新链（agent-runtime main 角色） | **951 字符 ≈ 451 token 估算** | GET /internal/diag/prompt-report（切换当日实测） |

**削减 97%**。分节：platform-min 230 / persona 81 / voice-rules 501 / context 53 / memory 36（按需检索注入）。

## 2. 首字延迟（TTFB）

| 场景 | TTFB | 说明 |
|---|---|---|
| 纯聊天轮（glm-5.3-flashx） | ≈1.2s | 直连智谱，流式 |
| 带工具轮（get_weather 全链） | ≈13.7s | 含模型思考 + 工具执行（open-meteo 外呼）+ 二轮生成；与旧链工具轮同构 |
| 旧链基线 | 不可测 | 其默认模型硅基流动 402 欠费——这正是切换的动因，旧链当前根本完不成一轮 |

模型定位（models.json，本地文件不入库）：main/worker=glm-5.3-flashx（text）、screen_vlm=glm-5.3-flash（image）。
硅基流动充值后如需切回 DeepSeek-V4-Flash：改 models.json 三个角色即可。

## 3. 任务成功率与生命周期

- Worker 全链路冒烟（真实服务）：TaskExecutor(ts) → POST /v1/worker/runs → LangGraph(plan→act→verify→outcome) → SSE → L1 验收 → completed 落库，**1/1 通过**（scripts/smoke_worker_e2e.py）。
- 审批中断→恢复：`test_worker_ts_backend.py`（stub SSE 全链路 13 测）+ `worker-runtime.test.ts`（fake model，interrupt/resume/cancel/checkpoint 复活 8 测）全绿。
- 长期成功率：待线上累积（bridge audit jsonl + tasks.jsonl 可查）。

## 4. 记忆（Phase 4）

- bridge `/internal/memory/search`：多关键词打分检索，包 ≤8 条 × 500 字，HTTP 端到端验证通过。
- 导入链：`scripts/import_openclaw_memory.py stage [--source] → 人工审阅 → commit --approve-all`；
  已导入仓库工作区 7 条 + home 工作区 23 条（data/memory/memory.sqlite3，36 条记录）。
- Main Agent 每轮按需检索 ≤3 条注入 MEMORY 段（失败/超时不阻断语音轮）。

## 5. 生产拓扑（切换后）

```
VoiceDaemon(schtasks GuanjiaVoice, 18795 控制口)
  ├─ 大脑: http://127.0.0.1:18800/v1/chat/completions（agent-runtime, Bearer=runtime.json token）
  ├─ 审批: bridge http://127.0.0.1:18801 /latest /decide（Bearer 同上）
  └─ 任务: GUANJIA_WORKER_BACKEND=ts → AgentRuntimeAdapter → 18800 /v1/worker/*
guard 守护: voice-supervisor.ps1 每 2s 探测 18800/18801，掉线自动拉起（run-agent-runtime.vbs / run-bridge.vbs）
OpenClaw: 网关进程已停（schtasks /End 成功）
```

## 6. 验收测试基线（切换当日全绿）

- TS：vitest 69 测（contracts 18 / registry 13 / main-agent 15 / server 10 / worker 8 / prompts 6 / spike 3 …）+ tsc build 零错误
- Python：test_bridge_service(73)、test_worker_ts_backend、test_task_executor_ts_backend(13)、test_memory_service(15)、test_task_executor（openclaw 路径回归）、test_agent_runtime_contracts(8)、test_p2_text_brain、test_p21_events

## 7. 遗留与主人待办

1. **OpenClaw Gateway 计划任务仍 enabled**（DISABLE 需管理员，本会话无提权）。当前进程已停，但重启电脑会再拉起——不碍事（大脑已不指它），要彻底退役请以管理员运行：
   `schtasks /Change /TN "OpenClaw Gateway" /DISABLE`
2. 硅基流动 402：如需 DeepSeek-V4-Flash 主模型，充值后改 models.json。
3. 面板 prompt 抽屉仍编辑 OpenClaw workspace 文件（惰性，不影响其他功能），是否接新 prompt 源待定。
4. `memory/openclaw_adapter.py` 纯 DTO 模块已无生产调用点，随 openclaw/ 目录 30 天后一并归档。
