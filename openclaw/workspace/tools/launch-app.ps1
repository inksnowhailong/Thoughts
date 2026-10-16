<#
.SYNOPSIS
  管家 L1 启动器：只启动 workspace\apps.json 映射里登记过的程序或链接。
.DESCRIPTION
  exec 审批白名单只放行本脚本（且参数形如 -Name <英文短名>），
  因此"能启动什么"完全由 apps.json 决定，改映射不用改审批。
.PARAMETER Name
  apps.json 里的键（如 lol），也接受 names 里的别名（大小写不敏感）。
.OUTPUTS
  一行 JSON：{ok, app, target} 或 {ok:false, error, ...}
#>
param([Parameter(Mandatory = $true)][string]$Name)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Out-Result($obj, [int]$code) {
    $obj | ConvertTo-Json -Compress
    exit $code
}

$mapPath = Join-Path $PSScriptRoot '..\apps.json'
$map = Get-Content $mapPath -Raw -Encoding UTF8 | ConvertFrom-Json
$key = $Name.Trim().ToLower()

# apps.json 的键名或任一别名精确命中优先。
$hit = $null
foreach ($p in $map.apps.PSObject.Properties) {
    $aliases = @($p.Name) + @($p.Value.names) | ForEach-Object { "$_".ToLower() }
    if ($aliases -contains $key) { $hit = $p; break }
}
if (-not $hit) {
    # 回退只索引同一份 apps.json：不会把开始菜单/注册表里的任意程序变成可启动目标。
    # 部署位置是 workspace\tools，不能从脚本目录反推项目根；只接受受控的项目根。
    $projectRoot = $null
    foreach ($candidate in @($env:GUANJIA_PROJECT_ROOT, 'F:\coding\guanjia')) {
        if (-not $candidate) { continue }
        $root = $candidate.TrimEnd('\', '/')
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $python = Join-Path $root '.venv\Scripts\python.exe'
        $index = Join-Path $root 'src\guanjia\app_index.py'
        if ((Test-Path -LiteralPath $python -PathType Leaf) -and
            (Test-Path -LiteralPath $index -PathType Leaf)) {
            $projectRoot = $root
            break
        }
    }
    if (-not $projectRoot) {
        Out-Result @{ ok = $false; error = 'not_in_map'; name = $Name; known = @($map.apps.PSObject.Properties.Name) } 2
    }
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = Join-Path $projectRoot 'src'
        $match = & $python -m guanjia.app_index --apps-json $mapPath --query $Name 2>$null | Select-Object -Last 1 | ConvertFrom-Json
    } catch {
        $match = $null
    } finally {
        $env:PYTHONPATH = $oldPythonPath
    }
    if ($match.kind -eq 'ambiguous') {
        Out-Result @{ ok = $false; error = 'ambiguous'; name = $Name; candidates = @($match.candidates | ForEach-Object { $_.key }) } 2
    }
    if ($match.kind -ne 'unique' -or -not $match.candidates[0].key) {
        Out-Result @{ ok = $false; error = 'not_in_map'; name = $Name; known = @($map.apps.PSObject.Properties.Name) } 2
    }
    $hit = @($map.apps.PSObject.Properties | Where-Object { $_.Name -eq $match.candidates[0].key })[0]
}

$target = "$($hit.Value.target)"
if (-not $target) {
    Out-Result @{ ok = $false; error = 'no_target'; app = $hit.Name; note = $hit.Value.note } 3
}

if ($target -match '^[A-Za-z][A-Za-z0-9+.-]*://') {
    # steam:// / com.epicgames.launcher:// / http(s):// 交给系统协议处理
    Start-Process $target
} else {
    if (-not (Test-Path $target)) {
        Out-Result @{ ok = $false; error = 'missing_file'; app = $hit.Name; target = $target } 4
    }
    Start-Process -FilePath $target -WorkingDirectory (Split-Path $target)
}

# 拉起请求不等于打开成功：最多等 3 秒，只靠进程名或窗口标题核实。
$exeName = if ($target -match '\.exe$') { [IO.Path]::GetFileNameWithoutExtension($target) } else { '' }
$verified = $false
for ($i = 0; $i -lt 3 -and -not $verified; $i++) {
    Start-Sleep -Seconds 1
    if ($exeName -and (Get-Process -Name $exeName -ErrorAction SilentlyContinue)) { $verified = $true }
    if (-not $verified) {
        $window = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*$($hit.Name)*" } | Select-Object -First 1
        if ($window) { $verified = $true }
    }
}
if (-not $verified) {
    Out-Result @{ ok = $false; error = 'unverified'; app = $hit.Name; target = $target } 5
}

Out-Result @{ ok = $true; app = $hit.Name; target = $target; verified = $true } 0
