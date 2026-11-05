<#
.SYNOPSIS
    并行跑 tests\ 下的 check() 风格测试脚本，只输出一行一个文件的汇总。
.EXAMPLE
    scripts\run-tests.ps1                      # 全量（提交前跑一次）
    scripts\run-tests.ps1 -Pattern wake,asr    # 只跑文件名含 wake 或 asr 的（改代码时用）
#>
param(
    [string[]]$Pattern = @(),
    [int]$Jobs = 6
)

$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'tmp\test-runs'
New-Item -ItemType Directory -Force $logDir | Out-Null

$Pattern = @($Pattern | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$files = Get-ChildItem (Join-Path $root 'tests') -Filter 'test_*.py' | Sort-Object Name
if ($Pattern.Count) { $files = $files | Where-Object { $n = $_.Name; @($Pattern | Where-Object { $n -like "*$_*" }).Count -gt 0 } }

$t0 = Get-Date
$running = @()
$results = @()
foreach ($f in $files) {
    while (@($running | Where-Object { -not $_.P.HasExited }).Count -ge $Jobs) { Start-Sleep -Milliseconds 200 }
    $log = Join-Path $logDir ($f.BaseName + '.txt')
    $p = Start-Process -FilePath $py -ArgumentList '-X', 'utf8', $f.FullName -WorkingDirectory $root -NoNewWindow -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError ($log + '.err')
    $null = $p.Handle  # 不先取句柄，Start-Process 的 ExitCode 会是空
    $running += [pscustomobject]@{ P = $p; F = $f; Log = $log; T = Get-Date }
}
foreach ($r in $running) {
    $r.P.WaitForExit()
    $txt = [IO.File]::ReadAllText($r.Log, [Text.Encoding]::UTF8)
    $pass = ([regex]::Matches($txt, '(?m)^PASS ')).Count
    $fail = ([regex]::Matches($txt, '(?m)^FAIL ')).Count
    $results += [pscustomobject]@{ Name = $r.F.Name; Rc = $r.P.ExitCode; Pass = $pass; Fail = $fail; Log = $r.Log }
}

# agent-runtime (TypeScript) 测试层：vitest 全量并入同一汇总；-Pattern 时仅在模式含 runtime 才跑
$tsTokens = 'agent_runtime runtime contracts spike prompts compiler'
$runTs = (-not $Pattern.Count) -or (@($Pattern | Where-Object { $tsTokens -like ('*' + $_.Replace('-', '_') + '*') }).Count -gt 0)
$rtRoot = Join-Path $root 'agent-runtime'
if ($runTs -and (Test-Path (Join-Path $rtRoot 'node_modules'))) {
    $tsLog = Join-Path $logDir 'agent-runtime-vitest.txt'
    $p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c npm test --silent' -WorkingDirectory $rtRoot -NoNewWindow -PassThru `
        -RedirectStandardOutput $tsLog -RedirectStandardError ($tsLog + '.err')
    $null = $p.Handle
    $p.WaitForExit()
    $tsTxt = [IO.File]::ReadAllText($tsLog, [Text.Encoding]::UTF8)
    $tsTxt = $tsTxt -replace ([string][char]27 + '\[[0-9;]*m'), ''  # 剥离 ANSI 颜色码
    $tsPass = [regex]::Match($tsTxt, 'Tests\s+(\d+) passed(?:\s+\((\d+)\))?\s+').Groups[1].Value
    $tsFail = [regex]::Match($tsTxt, 'Tests\s+\d+ passed\s+(\d+) failed').Groups[1].Value
    $results += [pscustomobject]@{ Name = 'agent-runtime (vitest)'; Rc = $p.ExitCode; Pass = [int]$tsPass; Fail = [int]$tsFail; Log = $tsLog }
}

$bad = @($results | Where-Object { $_.Rc -ne 0 -or $_.Fail -gt 0 })
foreach ($r in $results) { '{0,-34} rc={1} pass={2} fail={3}' -f $r.Name, $r.Rc, $r.Pass, $r.Fail }
foreach ($r in $bad) {
    "---- $($r.Name)"
    Get-Content $r.Log -Encoding UTF8 | Where-Object { $_ -match '^FAIL ' } | Select-Object -First 10
    Get-Content ($r.Log + '.err') -Encoding UTF8 -Tail 10
}
'TOTAL files={0} bad={1} elapsed={2:N0}s' -f $results.Count, $bad.Count, ((Get-Date) - $t0).TotalSeconds
if ($bad.Count) { exit 1 }
