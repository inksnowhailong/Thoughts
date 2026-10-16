<#
.SYNOPSIS
    在家里电脑（ssh home）上跑一段 PowerShell，并把输出按 UTF-8 取回本机打印。
    绕开 cmd 吃管道符、GBK 乱码、PowerShell 吞引号。短脚本一趟 ssh 完成；长脚本自动改走「scp 上传 → 执行 → scp 取回」。

.EXAMPLE
    & gj.ps1 'Get-Process python* | Select Id,StartTime'
    & gj.ps1 -File .\deploy.ps1
    & gj.ps1 'scripts\logq.cmd --recent 10'
#>
param(
    [Parameter(Position = 0)][string]$Script,
    [string]$File,
    [string]$Cwd = 'F:\coding\guanjia'
)

$ErrorActionPreference = 'Stop'
if ($File) { $Script = [IO.File]::ReadAllText((Resolve-Path $File)) }
if (-not $Script) { throw '需要 -Script 或 -File' }
$ssh = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20')

# 远程包装：在指定目录执行、合并所有输出流，结果以 UTF-8 的 base64 打到屏幕（纯 ASCII，不怕 GBK）
function New-Wrapper([string]$body) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
    @"
`$ErrorActionPreference = 'Continue'
Set-Location '$Cwd'
`$__s = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$b64'))
`$__o = & ([scriptblock]::Create(`$__s)) *>&1 | Out-String -Width 400
'GJ-BEGIN'; [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(`$__o)); 'GJ-END'
"@
}

function Read-Result($lines) {
    $text = ($lines | ForEach-Object { "$_" }) -join "`n"
    $m = [regex]::Match($text, 'GJ-BEGIN\s+([A-Za-z0-9+/=\s]*?)\s*GJ-END')
    if (-not $m.Success) { throw "远程没有返回结果：$($text.Substring(0, [Math]::Min(400, $text.Length)))" }
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(($m.Groups[1].Value -replace '\s', '')))
}

$wrapper = New-Wrapper $Script
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapper))

if ($enc.Length -lt 7500) {
    # cmd 命令行上限 8191 字符，短脚本直接 -EncodedCommand 一趟完成
    Read-Result (ssh @ssh home "powershell -NoProfile -EncodedCommand $enc" 2>&1)
} else {
    $id = [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $local = Join-Path $env:TEMP "gj-$id.ps1"
    [IO.File]::WriteAllText($local, $wrapper + "`nRemove-Item -LiteralPath `$PSCommandPath -Force", [Text.UTF8Encoding]::new($true))
    scp @ssh -q $local "home:F:/coding/guanjia/tmp/gj-$id.ps1"
    if ($LASTEXITCODE -ne 0) { throw 'scp 上传失败（家里电脑可能睡眠/掉线）' }
    Remove-Item $local -Force
    Read-Result (ssh @ssh home "powershell -NoProfile -ExecutionPolicy Bypass -File F:\coding\guanjia\tmp\gj-$id.ps1" 2>&1)
}