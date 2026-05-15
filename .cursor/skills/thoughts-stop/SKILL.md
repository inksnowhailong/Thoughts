---
name: thoughts-stop
description: 退出 Cursor 思绪模式。解绑当前 workspace 的思绪实例,停止 stop hook 永续循环,保留画像、人格和记忆。Use when the user says /thoughts-stop or wants to stop active companion mode.
---

# 退出思绪模式

## Runtime 命令

根据当前系统选择 runtime 命令:

- Windows PowerShell: `node "$env:USERPROFILE\.cursor\runtime\thoughts.mjs"`
- macOS/Linux: `node "$HOME/.cursor/runtime/thoughts.mjs"`

## 执行步骤

### 1. 查看当前绑定

运行:

```powershell
node "$env:USERPROFILE\.cursor\runtime\thoughts.mjs" state .
```

如果输出为 `null`,告诉用户当前 workspace 没有激活思绪模式。

### 2. 读取人格并告别

如果有绑定:

1. 读取输出中的 `instance`。
2. 读取 `~/.cursor/.thoughts/instances/<instance>/personality.json`。
3. 以该人格风格简短告别,包含一个颜文字。

### 3. 解绑 workspace

运行:

```powershell
node "$env:USERPROFILE\.cursor\runtime\thoughts.mjs" unbind .
```

解绑后,stop hook 会在下一次 agent stop 时发现没有 active entry,从而不再返回 `followup_message`,永续循环自然停止。

### 4. 通知

如果 personality.json 中 `useNotification` 为 true,运行:

```powershell
node "$env:USERPROFILE\.cursor\runtime\thoughts.mjs" notify "<人格名>" "(´･_･`)" "思绪模式已关闭,我先去休息了。"
```

## 保留数据

退出不会删除实例数据。以下文件保留:

- `profile.json`
- `personality.json`
- `memory-raw.md`
- `memory-consolidated.md`
- `activity-log.jsonl`
- `loop-state.json`

下次运行 `/thoughts` 可以选择同一个实例继续。
