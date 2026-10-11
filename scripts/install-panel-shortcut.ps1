# 在桌面放三个「思绪」快捷方式：思绪面板（双击：语音没跑就先拉起，再用浏览器打开面板）、思绪·开始语音、思绪·停止语音。
# 只新建不覆盖：桌面上已有同名快捷方式（可能是主人自己改过的）就原样保留；要重建请先手动删掉它。
# 改名迁移（二代显示名「管家」→「思绪」）：旧名「管家面板 / 管家·开始语音 / 管家·停止语音」的目标与现在要建的一致时，
# 直接改名成新名（图标等原样保留）；新名已存在则删掉目标相同的旧快捷方式；旧的被主人改过目标就不动它。
# 图标用托盘同款绿色圆点，生成到 data\guanjia.ico（data/ 不进仓库；已有就不重画）。
param(
    [string]$Desktop = [Environment]::GetFolderPath('Desktop'),
    [string]$Repo = 'F:\coding\guanjia',
    [string]$Python = 'F:\coding\guanjia\.venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
$ico = Join-Path $Repo 'data\guanjia.ico'
if (-not (Test-Path -LiteralPath $ico)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $ico) | Out-Null
    & $Python -c "import sys; from guanjia.tray import make_icon_image; make_icon_image((46, 160, 67), 256).save(sys.argv[1], sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])" $ico
    if (-not (Test-Path -LiteralPath $ico)) { throw "icon not created: $ico" }
}
$sh = New-Object -ComObject WScript.Shell
$schtasks = 'C:\Windows\System32\schtasks.exe'

# 新名 / 旧名 / 目标 / 参数 / 说明；图标 $true = 用托盘同款图标（面板快捷方式）
$items = @(
    @{ New = '思绪面板'; Old = '管家面板'; Target = 'C:\Windows\System32\wscript.exe'; Args = '//B ' + (Join-Path $Repo 'scripts\open-panel.vbs');
       Desc = '思绪面板：控制与调试（语音没跑会先拉起）'; Icon = $true },
    @{ New = '思绪·开始语音'; Old = '管家·开始语音'; Target = $schtasks; Args = '/Run /TN GuanjiaVoice'; Desc = '启动思绪语音'; Icon = $false },
    @{ New = '思绪·停止语音'; Old = '管家·停止语音'; Target = $schtasks; Args = '/End /TN GuanjiaVoice'; Desc = '停止思绪语音'; Icon = $false }
)
foreach ($it in $items) {
    $lnk = Join-Path $Desktop ($it.New + '.lnk')
    $oldLnk = Join-Path $Desktop ($it.Old + '.lnk')
    $oldSame = $false
    if (Test-Path -LiteralPath $oldLnk) {
        $o = $sh.CreateShortcut($oldLnk)
        $oldSame = ($o.TargetPath -eq $it.Target -and $o.Arguments -eq $it.Args)
    }
    if (Test-Path -LiteralPath $lnk) {
        "shortcut exists, left unchanged: $lnk"
        if ($oldSame) { Remove-Item -LiteralPath $oldLnk; "old shortcut removed: $oldLnk" }
        continue
    }
    if ($oldSame) {
        Move-Item -LiteralPath $oldLnk -Destination $lnk
        $s = $sh.CreateShortcut($lnk)
        $s.Description = $it.Desc
        $s.Save()
        "shortcut renamed: $oldLnk -> $lnk"
        continue
    }
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = $it.Target
    $s.Arguments = $it.Args
    if ($it.Icon) { $s.WorkingDirectory = $Repo; $s.IconLocation = "$ico,0" } else { $s.WindowStyle = 7 }
    $s.Description = $it.Desc
    $s.Save()
    "shortcut=$lnk"
    "target=$($s.TargetPath) $($s.Arguments)"
}
