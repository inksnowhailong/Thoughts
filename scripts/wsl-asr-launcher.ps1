# FireRedASR2 GPU 识别服务开机自启启动器（GuanjiaASR-WSL 计划任务 → run-wsl-asr.vbs → 本脚本；全程隐藏窗口）
#
# 结构与 wsl-tts-launcher.ps1 相同（TTS 开机自启踩过的坑：登录后 10 秒 Windows 网络/Docker Desktop/WSL 虚拟机还在变动，
# wsl.exe 会直接以 -1 退出，没有重试也没有日志），所以同样带重试地启动 WSL 里的服务，每次尝试都写日志：
#   - 开始前端口已经在监听（手动起过）→ 什么都不做，退出 0
#   - 每次尝试：启动 wsl.exe；StartTimeoutSec 内端口监听了 = 成功（模型加载约 20~60 秒，服务加载完才开始监听），之后一直等 wsl.exe 结束
#     （任务保持 Running；MultipleInstancesPolicy=IgnoreNew 靠它防重复启动）
#   - wsl.exe 提前退出、或超时一直没监听 → 记日志，等 IntervalSec 秒后重试，最多 MaxAttempts 次，用完记 gave up，退出 1
#   - 起来之后服务异常退出（被 OOM 杀、崩溃）→ 同样算一次失败并重启（TTS 启动器没有这一条，这里加上）
# 它是语音的「可选依赖」：没起来语音照常启动，二次识别走 SenseVoice 回退（见 asr_final.py），语音看门狗只会在起不来时顺手拉一次任务。
#
# 服务本体：scripts/asr_server.py，跑在 WSL 的独立 venv ~/guanjia-asr/venv（不碰 IndexTTS 的环境）。
# 为什么用 base64 传启动命令：不依赖任何 .sh 文件（git 的 autocrlf 会把 .sh 变成 CRLF 让 bash 报错），也躲开多层引号。
# 命令内容（可 base64 -d 查看）：
#   echo 500 > /proc/self/oom_score_adj   (WSL 内存吃紧时先杀 ASR、别杀 TTS)
#   export GUANJIA_ASR_PORT=18798 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
#   exec $HOME/guanjia-asr/venv/bin/python -u /mnt/f/coding/guanjia/scripts/asr_server.py > /mnt/f/coding/guanjia/tmp/asr_wsl.log 2>&1
param(
    [string]$Exe = 'wsl.exe',
    [string]$ExeArgs = '-d Ubuntu -- bash -c "echo ZWNobyA1MDAgPiAvcHJvYy9zZWxmL29vbV9zY29yZV9hZGoKZXhwb3J0IEdVQU5KSUFfQVNSX1BPUlQ9MTg3OTggUFlUT1JDSF9DVURBX0FMTE9DX0NPTkY9ZXhwYW5kYWJsZV9zZWdtZW50czpUcnVlCmV4ZWMgJEhPTUUvZ3VhbmppYS1hc3IvdmVudi9iaW4vcHl0aG9uIC11IC9tbnQvZi9jb2RpbmcvZ3VhbmppYS9zY3JpcHRzL2Fzcl9zZXJ2ZXIucHkgPiAvbW50L2YvY29kaW5nL2d1YW5qaWEvdG1wL2Fzcl93c2wubG9nIDI+JjEK | base64 -d | bash"',
    [int]$Port = 18798,
    [int]$MaxAttempts = 5,
    [int]$IntervalSec = 30,
    [int]$StartTimeoutSec = 240,
    [int]$PollSec = 3,
    [string]$LogPath = 'F:\coding\guanjia\tmp\asr-autostart.log',
    [string]$ServerLog = 'F:\coding\guanjia\tmp\asr_wsl.log'
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
    $outF = Join-Path $LogDir "asr-launch-$PID.out"
    $errF = Join-Path $LogDir "asr-launch-$PID.err"
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
        if ($code -eq 0) { exit 0 }
        # 起来之后异常退出（WSL 内存被 TTS 占满时被 OOM killer 杀掉、崩溃、被手动 pkill）：算一次失败，隔一会重启；
        # 语音那边在这期间走 SenseVoice 回退，服务起来后探活自动恢复
        if ($i -lt $MaxAttempts) { Write-Log "attempt ${i}: server died, restart in ${IntervalSec}s"; Start-Sleep -Seconds $IntervalSec; continue }
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
        if ((Test-Path $ServerLog) -and (Get-Item $ServerLog).LastWriteTime -ge $t0) {
            $tail = Get-Content $ServerLog -Tail 6 -Encoding UTF8 -ErrorAction Stop | ForEach-Object { Clip $_ 200 }
            Write-Log ("attempt ${i}: asr_wsl.log tail: " + ($tail -join ' | '))
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
