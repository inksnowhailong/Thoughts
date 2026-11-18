# 思绪（家庭语音管家）

当前系统由 Windows Python 语音服务、TypeScript Agent Runtime 和 Python 审批桥组成。

## 当前运行链

- `src/guanjia/voice.py`：语音守护进程、音频输入输出和本机控制面板，控制口 `18795`。
- `agent-runtime/`：默认对话运行时与后台 Worker，基于 TypeScript/LangGraph，端口 `18800`。
- Agent Runtime 内置 SQLite Memory Engine：自动记录对话、召回相关记忆并处理记忆巩固；独立 API 默认监听 `127.0.0.1:18802`，数据存放于 `data/agent-runtime/memory-engine.sqlite`。
- Mission Control 运行观察面板：访问 `http://127.0.0.1:18800/mission-control` 查看真实对话 Trace、模型/记忆/工具 Span、执行时间轴和只读 Memory Explorer。
- `src/guanjia/talk_direct.py`：可选直连对话模式；设置 `GUANJIA_TALK_DIRECT=1` 后经本机智谱代理 `18793` 调用模型。
- `src/guanjia/bridge_service.py`：审批服务，端口 `18801`。
- `scripts/voice-supervisor.ps1`：守护语音服务及其依赖。

当前没有 OpenClaw 网关或微信入口。`OpenClaw Gateway` 计划任务已禁用。

`openclaw/` 和 `%USERPROFILE%\.openclaw\workspace` 是遗留目录名，不代表当前运行 OpenClaw。工作区提示词与工具仍有实际引用，迁移前须先替换这些路径。旧网关插件、部署脚本、微信入口和技能说明已从源码中移除。

Memory Engine 的详细接口、检索、生命周期与巩固说明见 [`agent-runtime/src/memory-engine/README.md`](agent-runtime/src/memory-engine/README.md)。配置 `SILICONFLOW_API_KEY` 后，可使用 SiliconFlow 完成记忆提取、评估与向量嵌入；原始对话内容会发送至该服务用于提取。

Mission Control 采集语音识别、主 Agent、Worker、模型、工具、Memory 检索与巩固事件，并支持历史回放、Artifact 查看/导出和追踪固定保留。Provider/HTTP 传输层真实载荷受模型 SDK 限制，面板会明确标记“未采集”，不以逻辑请求冒充传输数据。Trace 数据可能包含完整提示词、用户输入、模型输出及工具参数，请将本机 `data/agent-runtime/observability.sqlite` 和同目录 `artifacts` 视为私密数据。实现边界见 [`agent-runtime/src/observability/README.md`](agent-runtime/src/observability/README.md)。

## 开发与验证

要求 Python 3.12、uv、Node.js 22 或更新版本。

后续日常开发统一使用 `master` 分支。

```powershell
git switch master
git pull
uv sync
cd agent-runtime
npm ci
npm run build
npm test
```

运行 Python 与 TypeScript 测试：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-tests.ps1
```

本机模型配置、数据、模型文件、虚拟环境和依赖不属于源码；不要批量删除 `data/`、`models/`、`.venv/` 或 `node_modules/`。

## 目录

- `src/guanjia/`：Python 语音、对话、任务、审批、记忆与视觉代码。
- `agent-runtime/`：TypeScript Main/Worker Agent。
- `openclaw/workspace/`：仍被代码引用的默认提示词、工作区工具与配置模板。
- `config/levels.json`：记忆授权规则的默认级别表；`config/family-facts.json` 是未自动加载的历史初始数据。
- `scripts/`、`tests/`：运维脚本和测试。
