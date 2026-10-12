# TTS 自启启动器用例：失败后重试直到监听、用完次数放弃、端口已在监听就不动、一直不监听会被结束并计一次失败。不碰 WSL / 真 TTS。
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

function Run-Launcher([string]$Name, [int]$Port, [string]$ChildArgs, [int]$Max, [int]$StartTimeout = 60) {
    $log = Join-Path $Dir "$Name.log"
    $a = "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`" -Exe `"$Py`" -ExeArgs `"$ChildArgs`" -Port $Port -MaxAttempts $Max -IntervalSec 1 -PollSec 1 -StartTimeoutSec $StartTimeout -LogPath `"$log`" -TtsLog `"$Dir\none.log`""
    $p = Start-Process powershell.exe -ArgumentList $a -PassThru -NoNewWindow -Wait
    return @{ Code = $p.ExitCode; Log = [string](Get-Content $log -Raw -Encoding UTF8) }
}

Check (Test-Path $Launcher) 'launcher script exists'

# 1. 前两次失败，第三次监听：重试 + 每次尝试都有日志
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

# 2. 一直失败：用完次数放弃，退出 1
$r = Run-Launcher 'giveup' 28792 "tests\fake_tts_child.py $Dir\c2.txt 28792 99" 3
Check ($r.Code -eq 1) 'give up: exits 1'
Check ((Get-Content "$Dir\c2.txt") -eq '3') 'give up: exactly MaxAttempts attempts'
Check ($r.Log -match 'gave up after 3 attempts') 'give up: logged'

# 3. 端口已经在监听：什么都不做
$l = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Parse('127.0.0.1'), 28793)
$l.Start()
try {
    $r = Run-Launcher 'already' 28793 "tests\fake_tts_child.py $Dir\c3.txt 28793 0" 3
} finally { $l.Stop() }
Check ($r.Code -eq 0 -and $r.Log -match 'already listening') 'already listening: nothing to do, exit 0'
Check (-not (Test-Path "$Dir\c3.txt")) 'already listening: server is not started again'

# 4. 一直不监听（子进程监听的是另一个端口）：超时后被结束，算一次失败
$r = Run-Launcher 'timeout' 28794 "tests\fake_tts_child.py $Dir\c4.txt 28795 0 30" 1 3
Check ($r.Code -eq 1) 'timeout: exits 1 after the only attempt'
Check ($r.Log -match 'no listener after' -and $r.Log -match 'FAILED exit=killed') 'timeout: wsl is stopped and the attempt logged as killed'

'ALL PASS test_tts_launcher'