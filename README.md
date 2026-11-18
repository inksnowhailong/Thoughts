# 思绪（家庭语音管家）

当前系统由 Windows Python 语音服务、TypeScript Agent Runtime 和 Python 审批桥组成。

## 当前运行链

- `src/guanjia/voice.py`：语音守护进程、音频输入输出和本机控制面板，控制口 `18795`。
- `agent-runtime/`：默认对话运行时与后台 Worker，基于 TypeScript/LangGraph，端口 `18800`。
- `src/guanjia/talk_direct.py`：可选直连对话模式；设置 `GUANJIA_TALK_DIRECT=1` 后经本机智谱代理 `18793` 调用模型。
- `src/guanjia/bridge_service.py`：审批服务，端口 `18801`。
- `scripts/voice-supervisor.ps1`：守护语音服务及其依赖。

当前没有 OpenClaw 网关或微信入口。`OpenClaw Gateway` 计划任务已禁用。

`openclaw/` 和 `%USERPROFILE%\.openclaw\workspace` 是遗留目录名，不代表当前运行 OpenClaw。工作区提示词与工具仍有实际引用，迁移前须先替换这些路径。旧网关插件、部署脚本、微信入口和技能说明已从源码中移除。

## 开发与验证

要求 Python 3.12、uv、Node.js 22 或更新版本。

```powershell
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
