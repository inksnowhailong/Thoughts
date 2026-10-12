# TTS 开机自启启动器（GuanjiaTTS-WSL 计划任务 → run-wsl-tts.vbs → 本脚本；全程隐藏窗口）
#
# 为什么要有它：计划任务在「登录 + 10 秒」只启动一次 wsl.exe。这时 Windows 网络（NTP/DNS 都失败）、Docker Desktop 自启
# （同一时刻也在拉 WSL 虚拟机）、WSL 虚拟机都还在变动，wsl.exe 直接以 -1 退出，没有任何重试、也没有日志，18791 就一直没人监听
# （2026-10-11、10-12 连续两天；手动 schtasks /Run 时系统已稳定，约 1 分钟起来）。
#
# 做法：带重试地启动 WSL 里的 TTS 服务，每次尝试都写日志（含 wsl.exe 的输出和 tts_wsl.log 的尾巴），直到 18791 监听：
#   - 开始前 18791 已经在监听（手动起过）→ 什么都不做，退出 0
#   - 每次尝试：启动 wsl.exe；StartTimeoutSec 内 18791 监听了 = 成功，之后本脚本一直等 wsl.exe 结束
#     （任务保持 Running，和原来 vbs 里 Run(...,True) 的行为一致；MultipleInstancesPolicy=IgnoreNew 靠它防重复启动）
#   - wsl.exe 提前退出，或超时一直没监听（此时结束这次的 wsl.exe）→ 记日志，等 IntervalSec 秒后重试，最多 MaxAttempts 次
#   - 用完次数仍没起来 → 记 gave up，退出 1（语音看门狗等 TTS 超时后还会再触发一次任务，见 voice-supervisor.ps1）
param(
    [string]$Exe = 'wsl.exe',
    [string]$ExeArgs = '-d Ubuntu -- bash /home/inksnow/guanjia-tts/run.sh',
    [int]$Port = 18791,
    [int]$MaxAttempts = 5,
    [int]$IntervalSec = 30,
    [int]$StartTimeoutSec = 240,
    [int]$PollSec = 3,
    [string]$LogPath = 'F:\coding\guanjia\tmp\tts-autostart.log',
    [string]$TtsLog = 'F:\coding\guanjia\tmp\tts_wsl.log'
)

$ErrorActionPreference = 'Continue'
$LogDir = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}' -f (Get-Date), $PID, $Message
    try { Add-Content -Path $LogPath -Value $line -Encoding UTF8 } catch {}  # 日志被占用写不进也不能影响启动
}

function Test-Port([int]$P) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { return ($c.ConnectAsync('127.0.0.1', $P).Wait(1000) -and $c.Connected) } catch { return $false } finally { $c.Close() }
}

# wsl.exe 的输出是 UTF-16LE（带一堆 NUL），其它程序是 UTF-8：按有没有 NUL 判断
function Read-Output([string]$Path) {
    try {
        if (-not (Test-Path $Path)) { return '' }
        $b = [IO.File]::ReadAllBytes($Path)
        if ($b.Length -eq 0) { return '' }
        $t = if ($b -contains 0) { [Text.Encoding]::Unicode.GetString($b) } else { [Text.Encoding]::UTF8.GetString($b) }
        return (($t -replace "`0", '') -replace '\s+', ' ').Trim()
    } catch { return '' }
}

function Clip([string]$s, [int]$n = 400) { if ($s.Length -le $n) { $s } else { $s.Substring(0, $n) + '...' } }

try { $up = [int]((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes } catch { $up = -1 }
Write-Log ("launcher start: port=$Port max=$MaxAttempts interval=${IntervalSec}s start_timeout=${StartTimeoutSec}s last_boot=${up}min ago (fast startup: 不等于刚登录)")

if (Test-Port $Port) { Write-Log "port $Port already listening, nothing to do"; exit 0 }

for ($i = 1; $i -le $MaxAttempts; $i++) {
    $outF = Join-Path $LogDir "tts-launch-$PID.out"
    $errF = Join-Path $LogDir "tts-launch-$PID.err"
    Remove-Item $outF, $errF -ErrorAction SilentlyContinue
    Write-Log "attempt $i/${MaxAttempts}: start $Exe $ExeArgs"
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $ExeArgs -NoNewWindow -PassThru -RedirectStandardOutput $outF -RedirectStandardError $errF
    } catch {
        Write-Log "attempt ${i}: start failed: $($_.Exception.Message)"
        if ($i -lt $MaxAttempts) { Start-Sleep -Seconds $IntervalSec }
        continue
    }
    $null = $p.Handle  # 先取句柄：PowerShell 5.1 不取句柄的话进程退出后 ExitCode 读出来是空
    $t0 = Get-Date
    $listening = $false
    $killed = $false
    while (-not $p.WaitForExit($PollSec * 1000)) {
        $el = [int]((Get-Date) - $t0).TotalSeconds
        if (Test-Port $Port) { $listening = $true; Write-Log "attempt ${i}: port $Port listening after ${el}s"; break }
        if ($el -ge $StartTimeoutSec) {
            Write-Log "attempt ${i}: no listener after ${el}s, stopping wsl pid=$($p.Id)"
            try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch { Write-Log "stop failed: $($_.Exception.Message)" }
            $killed = $true
            break
        }
    }
    if ($listening) {
        # 成功：服务在 wsl.exe 里跑着，一直等到它结束，任务保持 Running
        $p.WaitForExit()
        $code = $p.ExitCode
        Write-Log ("server exited code=$code after {0}s (attempt $i)" -f [int]((Get-Date) - $t0).TotalSeconds)
        Remove-Item $outF, $errF -ErrorAction SilentlyContinue
        exit $code
    }
    $ran = [int]((Get-Date) - $t0).TotalSeconds
    $code = if ($killed) { 'killed' } else { $p.ExitCode }
    if (-not $killed -and (Test-Port $Port)) {  # wsl.exe 退了但端口在监听（别处起来的服务）：算成功
        Write-Log "attempt ${i}: wsl exited code=$code after ${ran}s but port $Port is listening, done"
        Remove-Item $outF, $errF -ErrorAction SilentlyContinue
        exit 0
    }
    Write-Log ("attempt ${i}: FAILED exit=$code after ${ran}s; wsl stdout: [{0}] stderr: [{1}]" -f (Clip (Read-Output $outF)), (Clip (Read-Output $errF)))
    try {  # run.sh 里 python 的输出（CUDA 没就绪、端口占用等会在这里）；只有这次尝试期间更新过才贴
        if ((Test-Path $TtsLog) -and (Get-Item $TtsLog).LastWriteTime -ge $t0) {
            $tail = Get-Content $TtsLog -Tail 6 -Encoding UTF8 -ErrorAction Stop | ForEach-Object { Clip $_ 200 }
            Write-Log ("attempt ${i}: tts_wsl.log tail: " + ($tail -join ' | '))
        }
    } catch {}
    Remove-Item $outF, $errF -ErrorAction SilentlyContinue
    if ($i -lt $MaxAttempts) {
        Write-Log "attempt ${i}: retry in ${IntervalSec}s"
        Start-Sleep -Seconds $IntervalSec
    }
}
Write-Log "gave up after $MaxAttempts attempts, port $Port still not listening"
exit 1
