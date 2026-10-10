# 在桌面放「管家面板」快捷方式（双击：语音没跑就先拉起，再用浏览器打开面板）。重复运行会覆盖同名快捷方式。
# 图标用托盘同款绿色圆点，生成到 data\guanjia.ico（data/ 不进仓库）。
param(
    [string]$Desktop = [Environment]::GetFolderPath('Desktop'),
    [string]$Repo = 'F:\coding\guanjia',
    [string]$Python = 'F:\coding\guanjia\.venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
$ico = Join-Path $Repo 'data\guanjia.ico'
New-Item -ItemType Directory -Force -Path (Split-Path $ico) | Out-Null
& $Python -c "import sys; from guanjia.tray import make_icon_image; make_icon_image((46, 160, 67), 256).save(sys.argv[1], sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])" $ico
if (-not (Test-Path $ico)) { throw "icon not created: $ico" }
$lnk = Join-Path $Desktop '管家面板.lnk'
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = 'C:\Windows\System32\wscript.exe'
$s.Arguments = '//B ' + (Join-Path $Repo 'scripts\open-panel.vbs')
$s.WorkingDirectory = $Repo
$s.IconLocation = "$ico,0"
$s.Description = '管家面板：控制与调试（语音没跑会先拉起）'
$s.Save()
"shortcut=$lnk"
"target=$($s.TargetPath) $($s.Arguments)"
