# 思绪 — Windows 开机自启安装（任务计划程序）
# 用法: powershell -ExecutionPolicy Bypass -File install-win.ps1 -Instance 小思 [-Backend auto]
param(
    [Parameter(Mandatory = $true)][string]$Instance,
    [string]$Backend = 'auto'
)

$ErrorActionPreference = 'Stop'

# 定位 cli.mjs（本脚本在 runtime/install/ 下，上两级即仓库 runtime 目录）
$cli = Resolve-Path (Join-Path $PSScriptRoot '..\cli.mjs')
$node = (Get-Command node).Source
$taskName = "Thoughts-$Instance"

# 登录时启动，后台运行 daemon
$action = New-ScheduledTaskAction -Execute $node -Argument "`"$cli`" start $Instance --backend=$Backend"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Output "已注册开机自启任务: $taskName"
Write-Output "立即启动: Start-ScheduledTask -TaskName $taskName"
Write-Output "卸载:     Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
