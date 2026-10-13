# TTS 启动器 + 守护循环（GuanjiaTTS-WSL 计划任务 → run-wsl-tts.vbs → 本脚本；全程隐藏窗口）
#
# 第一代只管「开机启动重试」：计划任务在登录 +10 秒只启动一次 wsl.exe，这时网络 / Docker Desktop / WSL 虚拟机都还在变动，
# wsl.exe 会直接以 -1 退出（2026-10-11、10-12 连续两天）。第二代加上「运行期守护」：
# 2026-10-13 晚上 TTS 被 WSL 的 OOM killer 杀了三次，每次约 15 分钟没人拉起（启动器只管开机、进程死了就退出）。
#
# 现在的行为（仿 wsl-asr-launcher.ps1）：
#   - 开始前端口已经在监听（手动起过）→ 什么都不做，退出 0
#   - 启动前先等显存：空闲显存 < NeedFreeMB（游戏占着）就等，连续两次读数够了才启动（避免和游戏抢显存）；读不到显存就不拦
#   - 每次尝试：启动 wsl.exe；StartTimeoutSec 内端口监听 = 成功；wsl.exe 提前退出 / 超时没监听 = 失败，隔 IntervalSec 秒重试，
#     连续失败 MaxAttempts 次才放弃（退出 1）；成功起来一次就清零
#   - 运行期：一直等 wsl.exe 结束，同时每 ProbeSec 秒探一次端口，连续 ProbeFails 次不通（进程还在但服务死了/卡死）→ 杀掉重启
#   - 服务退出后：
#       退出码 0   → 正常停止，本脚本也退出 0
#       退出码 75  → 服务为了给游戏腾显存主动退出（见 tts_server/tts_guard.py），不算崩溃：记事件，等显存空出来再拉起
#       其它       → 崩溃 / OOM 被杀 / 被 pkill：记日志 + 事件，隔 IntervalSec 秒重启（不限次数）
#   - 每次退出都把 tts_wsl.log 备份成 tts_wsl.prev.log（服务每次启动会覆盖它，OOM 现场就丢了）并把尾部贴进日志
#   - 日志 tmp\tts-autostart.log；事件写 tmp\logs\events-tts-launcher-<日期>.jsonl（logq --comp tts 能查到）
#   - 任务保持 Running（MultipleInstancesPolicy=IgnoreNew 靠它防重复启动）
#
# 事件：tts.start / tts.up / tts.restart(warn) / tts.released / tts.wait_vram / tts.vram_ok / tts.port_down(warn) / tts.gave_up(error)
param(
    [string]$Exe = 'wsl.exe',
    [string]$ExeArgs = '-d Ubuntu -- bash /home/inksnow/guanjia-tts/run.sh',
    [int]$Port = 18791,
    [int]$MaxAttempts = 20,         # 连续「没起来」的次数上限
    [int]$IntervalSec = 30,
    [int]$StartTimeoutSec = 240,
    [int]$PollSec = 3,
    [int]$ProbeSec = 15,            # 运行期探端口的间隔
    [int]$ProbeFails = 3,           # 连续几次不通算服务死了
    [int]$MaxRuns = 0,              # 总启动次数上限，到了就退出 0（0 = 不限；测试 / 排障用）
    [int]$ReleasedExit = 75,        # 服务为游戏腾显存主动退出的退出码（tts_guard.EXIT_RELEASED）
    [int]$NeedFreeMB = 8192,        # 启动前要求的空闲显存（模型约 6GB + 余量）；0 = 不检查
    [int]$VramPollSec = 10,
    [string]$VramCmd = 'nvidia-smi.exe --query-gpu=memory.free --format=csv,noheader,nounits',
    [string]$KillCmd = 'wsl.exe -d Ubuntu -- pkill -f guanjia-tts/tts_server.py',
    [string]$LogPath = 'F:\coding\guanjia\tmp\tts-autostart.log',
    [string]$TtsLog = 'F:\coding\guanjia\tmp\tts_wsl.log',
    [string]$EventDir = 'F:\coding\guanjia\tmp\logs'
)

$ErrorActionPreference = 'Continue'
$LogDir = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}' -f (Get-Date), $PID, $Message
    try { Add-Content -Path $LogPath -Value $line -Encoding UTF8 } catch {}  # 日志被占用写不进也不能影响启动
}

# 事件写成一行 JSON（和 elog.py 同格式：ts / comp / level / ev / msg / data），logq 按文件名 events-*-<日期>*.jsonl 合并读取
function Write-Event([string]$Ev, [string]$Msg, [string]$Level = 'info', [hashtable]$Data = @{}) {
    try {
        New-Item -ItemType Directory -Force -Path $EventDir | Out-Null
        $rec = [ordered]@{
            ts    = [math]::Round([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0, 3)
            comp  = 'tts'
            level = $Level
            ev    = $Ev
            msg   = $Msg
        }
        if ($Data.Count) { $rec['data'] = $Data }
        $f = Join-Path $EventDir ('events-tts-launcher-{0:yyyy-MM-dd}.jsonl' -f (Get-Date))
        [IO.File]::AppendAllText($f, (($rec | ConvertTo-Json -Compress -Depth 4) + "`n"), (New-Object Text.UTF8Encoding $false))
    } catch {}  # 事件写不进不能影响守护
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

# 经 cmd.exe 跑一条命令（超时则放弃），返回标准输出文本；失败返回 $null
function Invoke-Cmd([string]$Cmd, [int]$TimeoutSec = 20) {
    $o = Join-Path $LogDir "tts-cmd-$PID.out"
    Remove-Item $o -ErrorAction SilentlyContinue
    try {
        $c = Start-Process -FilePath cmd.exe -ArgumentList "/c $Cmd" -NoNewWindow -PassThru -RedirectStandardOutput $o -RedirectStandardError "$o.err"
        $null = $c.Handle
        if (-not $c.WaitForExit($TimeoutSec * 1000)) { try { Stop-Process -Id $c.Id -Force } catch {}; return $null }
        $t = Read-Output $o
        Remove-Item $o, "$o.err" -ErrorAction SilentlyContinue
        return $t
    } catch { return $null }
}

# 全卡空闲显存 MB；读不到（没装 nvidia-smi、命令失败）返回 $null
function Get-FreeMB {
    $t = Invoke-Cmd $VramCmd
    if ($null -eq $t) { return $null }
    $n = $t -split '\s+' | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1
    if ($null -eq $n) { return $null }
    return [int]$n
}

# 启动前等空闲显存够（游戏占着显存时不抢）。第一次读数就够 → 立刻放行；被拦过 → 要连续两次够才放行（游戏退出时显存有抖动）
function Wait-Vram {
    if ($NeedFreeMB -le 0) { return }
    $blockedAt = $null; $okCount = 0; $lastLog = Get-Date
    while ($true) {
        $free = Get-FreeMB
        if ($null -eq $free) {
            if ($blockedAt) { Write-Log 'vram unreadable, go on'; Write-Event 'tts.vram_ok' 'vram unreadable, go on' 'warn' }
            return
        }
        if ($free -ge $NeedFreeMB) { $okCount++ } else { $okCount = 0 }
        if ($okCount -ge $(if ($blockedAt) { 2 } else { 1 })) {
            if ($blockedAt) {
                $w = [int]((Get-Date) - $blockedAt).TotalSeconds
                Write-Log "vram ok after ${w}s: free=${free}MB need=${NeedFreeMB}MB"
                Write-Event 'tts.vram_ok' "VRAM free again after ${w}s" 'info' @{ free_mb = $free; waited_sec = $w }
            }
            return
        }
        if (-not $blockedAt) {
            $blockedAt = Get-Date
            Write-Log "waiting for VRAM: free=${free}MB need=${NeedFreeMB}MB (game running?)"
            Write-Event 'tts.wait_vram' "waiting for free VRAM >= ${NeedFreeMB}MB (now ${free}MB)" 'info' @{ free_mb = $free; need_mb = $NeedFreeMB }
        } elseif (((Get-Date) - $lastLog).TotalSeconds -ge 300) {
            $lastLog = Get-Date
            Write-Log "still waiting for VRAM: free=${free}MB need=${NeedFreeMB}MB"
        }
        Start-Sleep -Seconds $VramPollSec
    }
}

# 服务退出后：把 tts_wsl.log 备份（下次启动会覆盖）并把尾部贴进日志
function Save-ServerLog([string]$Tag) {
    try {
        if (-not (Test-Path $TtsLog)) { return }
        $prev = [IO.Path]::ChangeExtension($TtsLog, '.prev.log')
        Copy-Item $TtsLog $prev -Force
        $tail = Get-Content $TtsLog -Tail 8 -Encoding UTF8 -ErrorAction Stop | ForEach-Object { Clip $_ 200 }
        Write-Log ("${Tag}: tts_wsl.log tail (saved to $([IO.Path]::GetFileName($prev))): " + ($tail -join ' | '))
    } catch {}
}

try { $up = [int]((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes } catch { $up = -1 }
Write-Log ("launcher start: port=$Port max=$MaxAttempts interval=${IntervalSec}s start_timeout=${StartTimeoutSec}s need_vram=${NeedFreeMB}MB last_boot=${up}min ago (fast startup: 不等于刚登录)")

if (Test-Port $Port) { Write-Log "port $Port already listening, nothing to do"; exit 0 }

$i = 0      # 连续尝试序号（成功起来一次就清零）
$runs = 0   # 总启动次数
while ($true) {
    $i++
    if ($i -gt $MaxAttempts) {
        Write-Log "gave up after $MaxAttempts attempts, port $Port still not listening"
        Write-Event 'tts.gave_up' "gave up after $MaxAttempts attempts" 'error' @{ attempts = $MaxAttempts }
        exit 1
    }
    if ($MaxRuns -gt 0 -and $runs -ge $MaxRuns) { Write-Log "reached MaxRuns=$MaxRuns, stop"; exit 0 }

    Wait-Vram
    $runs++
    $outF = Join-Path $LogDir "tts-launch-$PID.out"
    $errF = Join-Path $LogDir "tts-launch-$PID.err"
    Remove-Item $outF, $errF -ErrorAction SilentlyContinue
    Write-Log "attempt $i/${MaxAttempts}: start $Exe $ExeArgs"
    Write-Event 'tts.start' "starting TTS server (run $runs, attempt $i)" 'info' @{ run = $runs; attempt = $i }
    try {
        $p = Start-Process -FilePath $Exe -ArgumentList $ExeArgs -NoNewWindow -PassThru -RedirectStandardOutput $outF -RedirectStandardError $errF
    } catch {
        Write-Log "attempt ${i}: start failed: $($_.Exception.Message)"
        Start-Sleep -Seconds $IntervalSec
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
        $upAt = Get-Date
        Write-Event 'tts.up' "TTS server listening after $([int]($upAt - $t0).TotalSeconds)s" 'info' @{ run = $runs; sec = [int]($upAt - $t0).TotalSeconds }
        $i = 0  # 起来了：连续失败清零，之后的重启从 attempt 1 重新数
        # 运行期守护：等 wsl.exe 结束，同时每 ProbeSec 秒探一次端口
        $bad = 0; $lastProbe = Get-Date; $portDown = $false
        while (-not $p.WaitForExit($PollSec * 1000)) {
            if (((Get-Date) - $lastProbe).TotalSeconds -ge $ProbeSec) {
                $lastProbe = Get-Date
                if (Test-Port $Port) { $bad = 0 } else { $bad++ }
                if ($bad -ge $ProbeFails) {
                    Write-Log "port $Port unreachable $bad times in a row while wsl pid=$($p.Id) is alive, killing server"
                    Write-Event 'tts.port_down' "port $Port unreachable $bad times, killing server" 'warn' @{ probes = $bad }
                    $null = Invoke-Cmd $KillCmd
                    try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch {}
                    $portDown = $true
                    break
                }
            }
        }
        $null = $p.WaitForExit()
        $code = if ($portDown) { 'port_down' } else { $p.ExitCode }
        $ranSec = [int]((Get-Date) - $upAt).TotalSeconds
        Write-Log "server exited code=$code after ${ranSec}s (run $runs)"
        Remove-Item $outF, $errF -ErrorAction SilentlyContinue
        Save-ServerLog "run $runs"
        if ($code -eq 0) { exit 0 }  # 正常停止
        if ($code -eq $ReleasedExit) {
            # 服务为给游戏腾显存主动退出：不是崩溃；下一轮 Wait-Vram 会等显存空出来再拉起
            Write-Log "server released for gaming (exit $ReleasedExit), will restart when VRAM is free"
            Write-Event 'tts.released' 'TTS server exited to free VRAM for a game' 'info' @{ run = $runs; ran_sec = $ranSec }
            continue
        }
        $why = switch ("$code") { '9' { ' (SIGKILL: 多半是 WSL 内存不足被 OOM killer 杀，见 dmesg；10-13 21:13 那次 wsl.exe 就是这样返回 9)' } '137' { ' (SIGKILL: 多半是 OOM killer，见 dmesg)' } '143' { ' (SIGTERM / pkill)' } 'port_down' { ' (端口不通被杀)' } default { '' } }
        Write-Log "server died code=$code$why after ${ranSec}s, restart in ${IntervalSec}s"
        Write-Event 'tts.restart' "TTS server died (code=$code$why), restarting in ${IntervalSec}s" 'warn' @{ code = "$code"; run = $runs; ran_sec = $ranSec }
        Start-Sleep -Seconds $IntervalSec
        continue
    }

    # 没起来：wsl.exe 提前退出 / 超时没监听
    $ran = [int]((Get-Date) - $t0).TotalSeconds
    $code = if ($killed) { 'killed' } else { $p.ExitCode }
    if (-not $killed -and (Test-Port $Port)) {  # wsl.exe 退了但端口在监听（别处起来的服务）：算成功，不再守护
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
    Write-Event 'tts.restart' "TTS server failed to start (exit=$code), retry in ${IntervalSec}s" 'warn' @{ code = "$code"; attempt = $i; run = $runs }
    if ($i -lt $MaxAttempts) {
        Write-Log "attempt ${i}: retry in ${IntervalSec}s"
        Start-Sleep -Seconds $IntervalSec
    }
}
