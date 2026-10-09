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

# 键名或任一别名命中即算
$hit = $null
foreach ($p in $map.apps.PSObject.Properties) {
    $aliases = @($p.Name) + @($p.Value.names) | ForEach-Object { "$_".ToLower() }
    if ($aliases -contains $key) { $hit = $p; break }
}
if (-not $hit) {
    Out-Result @{ ok = $false; error = 'not_in_map'; name = $Name; known = @($map.apps.PSObject.Properties.Name) } 2
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

Out-Result @{ ok = $true; app = $hit.Name; target = $target } 0
