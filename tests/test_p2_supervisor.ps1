# P2 看门狗用例：退出码 3 立即重启、崩溃退避重启、0 退出；单实例。不碰语音和网络。
# 运行：powershell -NoProfile -ExecutionPolicy Bypass -File F:\coding\guanjia\tests\test_p2_supervisor.ps1
$ErrorActionPreference = 'Stop'
$Repo = 'F:\coding\guanjia'
$Dir = Join-Path $Repo 'tmp\p2-sup-test'
Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dir | Out-Null
$Py = Join-Path $Repo '.venv\Scripts\python.exe'
$Sup = Join-Path $Repo 'scripts\voice-supervisor.ps1'

function Check([bool]$Cond, [string]$What) {
    if ($Cond) { "PASS $What" } else { "FAIL $What"; exit 1 }
}

function Start-Sup([string]$ArgLine, [string]$LogFile, [switch]$NoWait) {
    $a = "-NoProfile -ExecutionPolicy Bypass -File `"$Sup`" -Exe `"$Py`" -ArgLine `"$ArgLine`" -LogPath `"$LogFile`" -MutexName Local\GjSupTest -SkipDeps -BackoffStartSec 1 -BackoffMaxSec 2"
    if ($NoWait) { return Start-Process powershell.exe -ArgumentList $a -PassThru -NoNewWindow }
    return Start-Process powershell.exe -ArgumentList $a -PassThru -NoNewWindow -Wait
}

Check (Test-Path $Sup) 'supervisor script exists'

# 1. 子进程依次退出 3、1、0
$log = Join-Path $Dir 'seq.log'
$p = Start-Sup "tests\fake_voice_child.py $Dir\counter.txt" $log
$text = Get-Content $log -Raw -Encoding UTF8
Check ($text -match 'voice exited code=3') 'exit code 3 seen'
Check ($text -match 'restart requested') 'code 3 restarts immediately'
Check ($text -match 'voice exited code=1') 'crash code 1 seen'
Check ($text -match 'restart in 1s') 'crash restarts after backoff'
Check ($text -match 'voice exited code=0') 'exit code 0 seen'
Check ($text -match 'quit requested') 'code 0 stops the supervisor'
Check ((Get-Content "$Dir\counter.txt") -eq '3') 'child started exactly 3 times'
Check ($p.ExitCode -eq 0) 'supervisor exits with 0'
Check (Test-Path (Join-Path $Dir 'voice-supervisor.pid')) 'pid file written next to the log'

# 2. 单实例：第一个在跑时第二个直接退出
Remove-Item "$Dir\counter.txt"
$a = Start-Sup "tests\fake_voice_child.py $Dir\counter.txt hold" (Join-Path $Dir 'a.log') -NoWait
Start-Sleep -Seconds 2
$null = Start-Sup "tests\fake_voice_child.py $Dir\counter.txt" (Join-Path $Dir 'b.log')
Check ((Get-Content (Join-Path $Dir 'b.log') -Raw -Encoding UTF8) -match 'another supervisor') 'second supervisor exits (single instance)'
$null = $a.WaitForExit(20000)
Check ((Get-Content (Join-Path $Dir 'a.log') -Raw -Encoding UTF8) -match 'quit requested') 'first supervisor finishes normally'

'ALL PASS test_p2_supervisor'
