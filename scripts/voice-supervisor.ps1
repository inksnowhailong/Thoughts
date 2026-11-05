# 语音守护进程看门狗（GuanjiaVoice 计划任务 → run-voice.vbs → 本脚本；全程隐藏窗口，跑在主人桌面会话里）
#
#   等依赖：网关 127.0.0.1:18789 能连上、TTS /health 返回 200（最多等 DepsTimeoutSec 秒，超时也照样起，托盘会显示出错）
#   拉起 python -m guanjia.voice，按退出码决定：
#     0    = 托盘「退出语音」→ 看门狗也退出，不再拉起
#     3    = 托盘「重启语音」→ 立即重启
#     其它 = 崩溃 → 退避 5s、10s、20s…最多 60s 后重启（稳定跑满 5 分钟后退避复位）
#   进程树：本进程绑进 KILL_ON_JOB_CLOSE 的 Job Object，子孙进程自动继承；本进程一退出，python 跟着结束（不按进程名杀）
#   计划任务被结束（schtasks /End 只杀 wscript）：每 2 秒看一眼父进程，父进程没了就退出
#   单实例：命名互斥量，第二个看门狗直接退出
param(
    [string]$Repo = 'F:\coding\guanjia-v2-development',
    [string]$Exe = '',
    [string]$ArgLine = '-m guanjia.voice -v --log-file F:/coding/guanjia-v2-development/tmp/voice.log',
    [string]$LogPath = 'F:\coding\guanjia-v2-development\tmp\voice-supervisor.log',
    [string]$MutexName = 'Local\GuanjiaVoiceSupervisor',
    [int]$DepsTimeoutSec = 180,
    [int]$GatewayPort = 18789,
    [string]$TtsHealthUrl = 'http://127.0.0.1:18791/health',
    [string]$TtsKickCmd = 'schtasks.exe /Run /TN GuanjiaTTS-WSL',  # 等 TTS 超时时主动拉起一次开机自启任务（只一次；不改任务定义）
    [int]$AsrPort = 18798,  # FireRedASR2 GPU 识别服务（WSL）：可选依赖，不等它，没起来只拉一次开机自启任务；语音走 SenseVoice 回退
    [string]$AsrKickCmd = 'schtasks.exe /Run /TN GuanjiaASR-WSL',
    [int]$BackoffStartSec = 5,
    [int]$BackoffMaxSec = 60,
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($Exe)) {
    $DevPython = Join-Path $Repo '.venv\Scripts\python.exe'
    if (Test-Path $DevPython) {
        $Exe = $DevPython
    } elseif (Test-Path 'F:\python\python.exe') {
        $Exe = 'F:\python\python.exe'
    } else {
        throw 'No development Python found. Refusing to use F:\coding\guanjia\.venv.'
    }
}
if ($Exe -like 'F:\coding\guanjia\*') {
    throw "Refusing to start modular main with production Python: $Exe"
}
$LogDir = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}' -f (Get-Date), $PID, $Message
    try { Add-Content -Path $LogPath -Value $line -Encoding UTF8 } catch {}  # 日志被占用时写不进也不能让看门狗退出
}

# 调参：tmp\voice.env 里的 GUANJIA_XXX=值 每次拉起 python 前重读，改完点托盘「重启语音」即生效（不用改本脚本、仓库不变脏）
function Import-VoiceEnv {
    $f = Join-Path $LogDir 'voice.env'
    if (-not (Test-Path $f)) { return }
    foreach ($l in Get-Content $f -Encoding UTF8) {
        if ($l -match '^\s*(GUANJIA_[A-Z0-9_]+)\s*=\s*(.*?)\s*$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
            Write-Log "env $($Matches[1])=$($Matches[2])"
        }
    }
}

$created = $false
$mutex = New-Object System.Threading.Mutex($true, $MutexName, [ref]$created)
if (-not $created) {
    Write-Log 'another supervisor is running, exit'
    exit 0
}

# Job Object：powershell 一死，唯一的 Job 句柄随之关闭，系统结束整棵进程树
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class GjJob {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr h, int c, IntPtr i, uint l);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    public static IntPtr Handle;
    public static bool Bind() {
        Handle = CreateJobObject(IntPtr.Zero, null);
        IntPtr buf = Marshal.AllocHGlobal(144);
        for (int i = 0; i < 144; i++) Marshal.WriteByte(buf, i, 0);
        Marshal.WriteInt32(buf, 16, 0x2000);
        bool ok = SetInformationJobObject(Handle, 9, buf, 144);
        Marshal.FreeHGlobal(buf);
        return ok && AssignProcessToJobObject(Handle, GetCurrentProcess());
    }
}
"@
Write-Log ('start job={0} exe={1} args={2}' -f [GjJob]::Bind(), $Exe, $ArgLine)
Set-Content -Path (Join-Path $LogDir 'voice-supervisor.pid') -Value $PID -Encoding ASCII

$ParentPid = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
$ParentStart = (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue).StartTime
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = (Join-Path $Repo 'src')
$env:GUANJIA_TTS_URL = 'http://127.0.0.1:18791/tts'
$env:GUANJIA_VOICE_MODEL = ''
$env:GUANJIA_ROOT = 'F:\coding\guanjia-v2-development'
$env:GUANJIA_MODULAR_MAIN_V1 = '1'
Set-Location $Repo

function Test-ParentAlive {
    $p = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    return [bool]($p -and $p.StartTime -eq $ParentStart)  # 比对启动时间，防 PID 被复用
}

function Test-Port([int]$Port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { return ($c.ConnectAsync('127.0.0.1', $Port).Wait(1000) -and $c.Connected) } catch { return $false } finally { $c.Close() }
}

# Wave1-C/Phase5（2026-11-05）：bridge(18801) 与 agent-runtime(18800) 随看门狗守护——
# 每次拉起语音前后都探测，掉线自动拉起（wscript 启动器见 scripts/run-*.vbs；
# 计划任务建不了的端口占用场景也安全：Test-Port 在就跳过）。
function Ensure-Service([int]$Port, [string]$Vbs, [string]$Name) {
    if (Test-Port $Port) { return }
    Write-Log "service down ($Name :$Port), starting $Vbs"
    try {
        Start-Process -FilePath 'wscript.exe' -ArgumentList "//B `"$Vbs`"" -WindowStyle Hidden | Out-Null
    } catch { Write-Log "start $Name failed: $($_.Exception.Message)" }
}
$BridgeVbs = Join-Path $Repo 'scripts\run-bridge.vbs'
$AgentRuntimeVbs = Join-Path $Repo 'scripts\run-agent-runtime.vbs'

function Test-Http([string]$Url) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Proxy = $null  # 不走系统代理（Clash）
        $req.Timeout = 2000
        $resp = $req.GetResponse()
        $ok = [int]$resp.StatusCode -eq 200
        $resp.Close()
        return $ok
    } catch { return $false }
}

# TTS 等了 DepsTimeoutSec 秒还没起来（开机自启失败的典型表现）：主动触发一次开机自启任务；本进程生命周期内只触发一次。
# 任务已经在 Running（启动器还在重试）时，schtasks /Run 会因 MultipleInstancesPolicy=IgnoreNew 被忽略，无害。
$script:TtsKicked = $false
function Invoke-TtsKick {
    if ($script:TtsKicked) { return }
    $script:TtsKicked = $true
    $outF = Join-Path $LogDir 'tts-kick.out'
    try {
        $k = Start-Process -FilePath cmd.exe -ArgumentList "/c $TtsKickCmd" -NoNewWindow -Wait -PassThru -RedirectStandardOutput $outF -RedirectStandardError (Join-Path $LogDir 'tts-kick.err')
        $txt = (([IO.File]::ReadAllText($outF, [Text.Encoding]::Default)) -replace '\s+', ' ').Trim()
        Write-Log "tts not ready: triggered once: $TtsKickCmd -> exit=$($k.ExitCode) $txt"
    } catch { Write-Log "tts kick failed: $($_.Exception.Message)" }
}

# ASR 是「可选依赖」：永远不等它。本进程生命周期内只检查/触发一次——端口没在监听就拉一次 GuanjiaASR-WSL 任务
# （已在 Running 时 schtasks /Run 因 IgnoreNew 被忽略，无害）。起不来语音照常启动，二次识别走 SenseVoice，起来后自动恢复（asr_final 探活）。
$script:AsrChecked = $false
function Invoke-AsrCheck {
    if ($script:AsrChecked) { return }
    $script:AsrChecked = $true
    if (Test-Port $AsrPort) { Write-Log "asr ready (port $AsrPort)"; return }
    $outF = Join-Path $LogDir 'asr-kick.out'
    try {
        $k = Start-Process -FilePath cmd.exe -ArgumentList "/c $AsrKickCmd" -NoNewWindow -Wait -PassThru -RedirectStandardOutput $outF -RedirectStandardError (Join-Path $LogDir 'asr-kick.err')
        $txt = (([IO.File]::ReadAllText($outF, [Text.Encoding]::Default)) -replace '\s+', ' ').Trim()
        Write-Log "asr not up (optional, not waiting): kicked once: $AsrKickCmd -> exit=$($k.ExitCode) $txt"
    } catch { Write-Log "asr kick failed: $($_.Exception.Message)" }
}

function Wait-Deps {
    if ($SkipDeps) { return }
    Invoke-AsrCheck
    $t0 = Get-Date
    while ($true) {
        $gw = Test-Port $GatewayPort
        $tts = Test-Http $TtsHealthUrl
        if ($gw -and $tts) { Write-Log 'deps ready (gateway, tts)'; return }
        $waited = [int]((Get-Date) - $t0).TotalSeconds
        if ($waited -ge $DepsTimeoutSec) {
            if (-not $tts) { Invoke-TtsKick }
            Write-Log ('deps not ready after {0}s (gateway={1} tts={2}), starting anyway' -f $waited, $gw, $tts)
            return
        }
        if (-not (Test-ParentAlive)) { Write-Log 'parent gone while waiting deps, exit'; exit 0 }
        Start-Sleep -Seconds 3
    }
}

# Start-Process -RedirectStandardError 每次拉起都会清空 voice-stderr.txt：上一个进程的崩溃堆栈必须先存档，否则下一次拉起就丢了
# （2026-10-11/12 事故：python 以退出码 1 退出，堆栈只在 stderr，被下一次拉起清空，事后 voice-stderr.txt 是空的）
$StderrPath = Join-Path $LogDir 'voice-stderr.txt'
function Save-Stderr([int]$Code, [switch]$Pre) {
    $abnormal = (-not $Pre) -and $Code -ne 0 -and $Code -ne 3  # 负数退出码（原生崩溃）也算异常
    try {
        if (-not (Test-Path $StderrPath)) { return }
        $text = [IO.File]::ReadAllText($StderrPath, [Text.Encoding]::UTF8)
        if ($text.Trim().Length -eq 0) {
            if ($abnormal) { Write-Log 'stderr empty (no traceback): see voice.log / events voice.exit' }
            return
        }
        $hist = Join-Path $LogDir 'voice-stderr.history.txt'
        Add-Content -Path $hist -Value ("===== {0:yyyy-MM-dd HH:mm:ss} code={1} =====`r`n{2}" -f (Get-Date), $Code, $text) -Encoding UTF8
        if ((Get-Item $hist).Length -gt 512KB) {  # 只留尾巴，别无限长大
            $all = [IO.File]::ReadAllText($hist, [Text.Encoding]::UTF8)
            [IO.File]::WriteAllText($hist, $all.Substring($all.Length - 200KB), [Text.UTF8Encoding]::new($true))
        }
        if ($abnormal) {  # 异常退出：把堆栈尾巴也写进看门狗日志，一份日志就能看全
            foreach ($l in ($text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 12)) {
                Write-Log ('stderr: ' + $l.Substring(0, [Math]::Min(300, $l.Length)))
            }
        }
        Clear-Content -Path $StderrPath
    } catch { Write-Log "save stderr failed: $($_.Exception.Message)" }
}

$backoff = $BackoffStartSec
while ($true) {
    Ensure-Service 18801 $BridgeVbs 'bridge'
    Ensure-Service 18800 $AgentRuntimeVbs 'agent-runtime'
    Save-Stderr 0 -Pre  # 上一个看门狗被杀时没来得及存档的 stderr
    Wait-Deps
    Import-VoiceEnv
    $t0 = Get-Date
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $ArgLine -WorkingDirectory $Repo -NoNewWindow -PassThru -RedirectStandardError (Join-Path $LogDir 'voice-stderr.txt')
    } catch {
        Write-Log "start failed: $($_.Exception.Message); retry in ${backoff}s"
        Start-Sleep -Seconds $backoff
        $backoff = [Math]::Min($backoff * 2, $BackoffMaxSec)
        continue
    }
    $null = $p.Handle  # 先取句柄：PowerShell 5.1 不取句柄的话进程退出后 ExitCode 读出来是空
    Write-Log "voice started pid=$($p.Id)"
    while (-not $p.WaitForExit(2000)) {
        if (-not (Test-ParentAlive)) { Write-Log 'parent gone (task ended), exit; job closes and takes python with it'; exit 0 }
        Ensure-Service 18801 $BridgeVbs 'bridge'
        Ensure-Service 18800 $AgentRuntimeVbs 'agent-runtime'
    }
    $code = $p.ExitCode
    $ran = [int]((Get-Date) - $t0).TotalSeconds
    Write-Log "voice exited code=$code after ${ran}s"
    Save-Stderr $code
    if ($code -eq 0) { Write-Log 'quit requested (tray), supervisor exit'; break }
    if ($code -eq 3) { Write-Log 'restart requested (tray)'; $backoff = $BackoffStartSec; continue }
    if ($ran -ge 300) { $backoff = $BackoffStartSec }
    Write-Log "restart in ${backoff}s"
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min($backoff * 2, $BackoffMaxSec)
}
$mutex.ReleaseMutex()






