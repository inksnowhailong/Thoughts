# 桌面「管家面板」快捷方式 → open-panel.vbs（隐藏窗口）→ 本脚本：
#   语音在跑（127.0.0.1:18795 在听）→ 直接用默认浏览器打开面板
#   语音没跑（在面板/托盘里退出过）→ 先启动计划任务 GuanjiaVoice，最多等 90 秒控制口起来再打开；等不到就弹一个提示框
# -DryRun：只打印要做的事，不启动任务、不开浏览器、不弹框（单测用）
param(
    [string]$Url = 'http://127.0.0.1:18795/panel',
    [int]$Port = 18795,
    [string]$TaskName = 'GuanjiaVoice',
    [int]$WaitSec = 90,
    [switch]$DryRun
)

function Test-Port([int]$P) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { return ($c.ConnectAsync('127.0.0.1', $P).Wait(800) -and $c.Connected) } catch { return $false } finally { $c.Close() }
}

function Open-Url {
    if ($DryRun) { "would open $Url"; return }
    Start-Process $Url
}

if (Test-Port $Port) { Open-Url; exit 0 }

if ($DryRun) { "would start task $TaskName"; exit 0 }
try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
$t0 = Get-Date
while (((Get-Date) - $t0).TotalSeconds -lt $WaitSec) {
    Start-Sleep -Seconds 2
    if (Test-Port $Port) { Open-Url; exit 0 }
}
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show("语音进程没起来（等了 $WaitSec 秒）。可以看 F:\coding\guanjia\tmp\voice-supervisor.log 和 voice.log。", '管家面板') | Out-Null
exit 1
