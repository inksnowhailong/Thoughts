# TTS 启动器 + 守护循环测试：开机重试、崩溃 / 为游戏退出 / 端口不通后的重启、启动前等显存、事件与日志备份。
# 全程用假 TTS 进程（tests\fake_tts_child.py）和假显存读数，不碰真 WSL / 真 TTS（18791）。
# 运行：powershell -NoProfile -ExecutionPolicy Bypass -File F:\coding\guanjia\tests\test_tts_launcher.ps1
$ErrorActionPreference = 'Stop'
$Repo = 'F:\coding\guanjia'
$Dir = Join-Path $Repo 'tmp\tts-launcher-test'
Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dir | Out-Null
$Py = Join-Path $Repo '.venv\Scripts\python.exe'
$Launcher = Join-Path $Repo 'scripts\wsl-tts-launcher.ps1'

function Check([bool]$Cond, [string]$What) {
    if ($Cond) { "PASS $What" } else { "FAIL $What"; exit 1 }
}

function Launcher-Args([string]$Name, [int]$Port, [string]$ChildArgs, [string]$Extra, [int]$Need = 0) {
    $log = Join-Path $Dir "$Name.log"
    $ev = Join-Path $Dir "$Name-events"
    "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`" -Exe `"$Py`" -ExeArgs `"$ChildArgs`" -Port $Port -IntervalSec 1 -PollSec 1 -NeedFreeMB $Need -LogPath `"$log`" -TtsLog `"$Dir\$Name-tts.log`" -EventDir `"$ev`" $Extra"
}

function Run-Launcher([string]$Name, [int]$Port, [string]$ChildArgs, [int]$Max, [int]$StartTimeout = 60, [string]$Extra = '') {
    $a = Launcher-Args $Name $Port $ChildArgs "-MaxAttempts $Max -StartTimeoutSec $StartTimeout $Extra"
    $p = Start-Process powershell.exe -ArgumentList $a -PassThru -NoNewWindow -Wait
    return (Read-Result $Name $p.ExitCode)
}

function Read-Result([string]$Name, $Code) {
    $log = Join-Path $Dir "$Name.log"
    $evText = ''
    $ev = Join-Path $Dir "$Name-events"
    if (Test-Path $ev) { $evText = (Get-ChildItem $ev -Filter 'events-tts-launcher-*.jsonl' | ForEach-Object { Get-Content $_.FullName -Raw -Encoding UTF8 }) -join "`n" }
    return @{ Code = $Code; Log = [string](Get-Content $log -Raw -Encoding UTF8); Events = $evText }
}

function Count([string]$Text, [string]$Pattern) { return ([regex]::Matches($Text, $Pattern)).Count }

Check (Test-Path $Launcher) 'launcher script exists'

# 1. 前两次失败、第三次成功 + 每次尝试都有日志
$r = Run-Launcher 'retry' 28791 "tests\fake_tts_child.py $Dir\c1.txt 28791 2 3" 5
Check ($r.Code -eq 0) 'retry: exits 0 once the server has run'
Check ((Get-Content "$Dir\c1.txt") -eq '3') 'retry: server started exactly 3 times (2 failures + 1 success)'
Check ($r.Log -match 'attempt 1/5: start') 'retry: attempt 1 logged'
Check ($r.Log -match 'attempt 1: FAILED exit=1') 'retry: failure of attempt 1 logged with exit code'
Check ($r.Log -match 'stderr: \[wsl: fake failure 1\]') 'retry: the child stderr text is in the log'
Check ($r.Log -match 'attempt 2: FAILED exit=1') 'retry: failure of attempt 2 logged'
Check ($r.Log -match 'attempt 2: retry in 1s') 'retry: waits between attempts'
Check ($r.Log -match 'attempt 3: port 28791 listening after') 'retry: attempt 3 success logged'
Check ($r.Log -match 'server exited code=0') 'retry: waits for the server to end (task stays Running)'

# 2. 一直失败：连续失败次数用完后放弃，退出 1
$r = Run-Launcher 'giveup' 28792 "tests\fake_tts_child.py $Dir\c2.txt 28792 99" 3
Check ($r.Code -eq 1) 'give up: exits 1'
Check ((Get-Content "$Dir\c2.txt") -eq '3') 'give up: exactly MaxAttempts attempts'
Check ($r.Log -match 'gave up after 3 attempts') 'give up: logged'
Check ($r.Events -match '"ev":"tts.gave_up"') 'give up: event recorded'

# 3. 端口已经在监听：什么都不做
$l = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Parse('127.0.0.1'), 28793)
$l.Start()
try {
    $r = Run-Launcher 'already' 28793 "tests\fake_tts_child.py $Dir\c3.txt 28793 0" 3
} finally { $l.Stop() }
Check ($r.Code -eq 0 -and $r.Log -match 'already listening') 'already listening: nothing to do, exit 0'
Check (-not (Test-Path "$Dir\c3.txt")) 'already listening: server is not started again'

# 4. 一直不监听：子进程监听了另一个端口，超时后被结束，算一次失败
$r = Run-Launcher 'timeout' 28794 "tests\fake_tts_child.py $Dir\c4.txt 28795 0 30" 1 3
Check ($r.Code -eq 1) 'timeout: exits 1 after the only attempt'
Check ($r.Log -match 'no listener after' -and $r.Log -match 'FAILED exit=killed') 'timeout: wsl is stopped and the attempt logged as killed'

# 5. 起来之后崩溃（退出码 1，模拟 OOM / 异常）：隔 IntervalSec 秒自动重启，不限次数（这里用 MaxRuns=3 收尾），每次记事件
Set-Content "$Dir\crash-tts.log" 'boom: pretend python traceback / last words' -Encoding UTF8
$r = Run-Launcher 'crash' 28796 "tests\fake_tts_child.py $Dir\c5.txt 28796 0 2 1" 20 60 '-MaxRuns 3'
Check ($r.Code -eq 0) 'crash restart: exits 0 once MaxRuns is reached'
Check ((Get-Content "$Dir\c5.txt") -eq '3') 'crash restart: server restarted after every crash (3 runs)'
Check ((Count $r.Log 'server died code=1') -eq 3) 'crash restart: each crash logged with its exit code'
Check ($r.Log -match 'server died code=1 after \d+s, restart in 1s') 'crash restart: waits IntervalSec before restarting'
Check ($r.Log -notmatch 'gave up') 'crash restart: never gives up after the server has come up once'
Check ((Count $r.Events '"ev":"tts.restart"') -eq 3) 'crash restart: one tts.restart event per crash'
Check ($r.Events -match '"level":"warn"' -and $r.Events -match '"code":"1"') 'crash restart: event is warn level and carries the exit code'
Check ((Count $r.Events '"ev":"tts.up"') -eq 3) 'crash restart: tts.up event after each start'
Check ((Test-Path "$Dir\crash-tts.prev.log") -and ((Get-Content "$Dir\crash-tts.prev.log" -Raw) -match 'boom')) 'crash restart: tts_wsl.log is saved as .prev.log before the next start overwrites it'
Check ($r.Log -match 'tts_wsl.log tail .*boom') 'crash restart: the log tail is copied into the launcher log'

# 6. 为给游戏腾显存主动退出（75）：不算崩溃，不隔 IntervalSec 睡眠，记 tts.released
$r = Run-Launcher 'released' 28797 "tests\fake_tts_child.py $Dir\c6.txt 28797 0 3 75" 20 60 '-MaxRuns 2'
Check ($r.Code -eq 0 -and (Get-Content "$Dir\c6.txt") -eq '2') 'released: restarted once after the release'
Check ((Count $r.Log 'released for gaming') -eq 2) 'released: logged as released for gaming'
Check ($r.Log -notmatch 'server died') 'released: not treated as a crash'
Check ((Count $r.Events '"ev":"tts.released"') -eq 2 -and $r.Events -notmatch '"ev":"tts.restart"') 'released: tts.released events, no tts.restart'

# 7. 进程还活着但端口不通：连续探测失败后杀掉并重启
$r = Run-Launcher 'portdown' 28798 "tests\fake_tts_child.py $Dir\c7.txt 28798 0 60 0 closeport" 20 60 "-MaxRuns 2 -ProbeSec 1 -ProbeFails 2 -KillCmd `"echo killed>>$Dir\kill.txt`""
Check ($r.Code -eq 0 -and (Get-Content "$Dir\c7.txt") -eq '2') 'port down: hung server killed and started again'
Check ($r.Log -match 'unreachable 2 times in a row') 'port down: probe failures logged'
Check ((Test-Path "$Dir\kill.txt") -and @(Get-Content "$Dir\kill.txt").Count -eq 2) 'port down: the kill command ran once per hang'
Check ((Count $r.Events '"ev":"tts.port_down"') -eq 2) 'port down: tts.port_down events'

# 8. 启动前等显存：游戏占着（空闲 1000MB < 8192MB）时不启动，空出来（连续两次读数够）才启动
Set-Content "$Dir\vram.txt" '1000' -Encoding ASCII
$a = Launcher-Args 'vram' 28799 "tests\fake_tts_child.py $Dir\c8.txt 28799 0 3" "-MaxAttempts 3 -VramPollSec 1 -VramCmd `"type $Dir\vram.txt`"" 8192
$lp = Start-Process powershell.exe -ArgumentList $a -PassThru -NoNewWindow
$null = $lp.Handle
Start-Sleep -Seconds 5
$mid = Read-Result 'vram' $null
Check ($mid.Log -match 'waiting for VRAM: free=1000MB need=8192MB') 'vram gate: waits while a game holds the VRAM'
Check (-not (Test-Path "$Dir\c8.txt")) 'vram gate: server is not started while VRAM is short'
Set-Content "$Dir\vram.txt" '13000' -Encoding ASCII
$done = $lp.WaitForExit(30000)
$r = Read-Result 'vram' $(if ($done) { $lp.ExitCode } else { -1 })
Check ($done -and $r.Code -eq 0) 'vram gate: launcher finishes after the VRAM is free'
Check ((Get-Content "$Dir\c8.txt") -eq '1') 'vram gate: server started once when VRAM got free'
Check ($r.Log -match 'vram ok after') 'vram gate: wait end logged'
Check ($r.Events -match '"ev":"tts.wait_vram"' -and $r.Events -match '"ev":"tts.vram_ok"') 'vram gate: wait start / end events'

# 9. 读不到显存（没装 nvidia-smi 等）：不拦，照常启动
$a = Launcher-Args 'novram' 28800 "tests\fake_tts_child.py $Dir\c9.txt 28800 0 3" "-MaxAttempts 3 -VramPollSec 1 -VramCmd `"echo no-gpu-here`"" 8192
$p = Start-Process powershell.exe -ArgumentList $a -PassThru -NoNewWindow -Wait
Check ($p.ExitCode -eq 0 -and (Get-Content "$Dir\c9.txt") -eq '1') 'vram unreadable: does not block the start'

'ALL PASS test_tts_launcher'
