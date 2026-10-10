# P2.1 面板快捷方式脚本用例：open-panel.ps1 的两条路（语音在跑 → 开网页；没跑 → 先拉起任务），install-panel-shortcut.ps1 生成快捷方式。
# 只用 -DryRun 和临时桌面目录：不启动任务、不开浏览器、不碰真实桌面。
# 运行：powershell -NoProfile -ExecutionPolicy Bypass -File F:\coding\guanjia\tests\test_p21_panel_scripts.ps1
param([string]$Repo = 'F:\coding\guanjia', [string]$Py = (Join-Path $Repo '.venv\Scripts\python.exe'))
$ErrorActionPreference = 'Stop'
$Open = Join-Path $Repo 'scripts\open-panel.ps1'
$Install = Join-Path $Repo 'scripts\install-panel-shortcut.ps1'

function Check([bool]$Cond, [string]$What) {
    if ($Cond) { "PASS $What" } else { "FAIL $What"; exit 1 }
}

Check (Test-Path $Open) 'open-panel.ps1 exists'
$out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Open -Port 18993 -DryRun
Check (($out -join ' ') -match 'would start task GuanjiaVoice') 'voice not running -> start the task first'

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 18992)
$listener.Start()
try {
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Open -Port 18992 -Url 'http://127.0.0.1:18992/panel' -DryRun
    Check (($out -join ' ') -match 'would open http://127.0.0.1:18992/panel') 'voice running -> open the panel'
} finally { $listener.Stop() }

$desk = Join-Path $env:TEMP ('gj-desk-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $desk | Out-Null
$null = & powershell -NoProfile -ExecutionPolicy Bypass -File $Install -Desktop $desk -Repo $Repo -Python $Py
$lnk = Join-Path $desk '管家面板.lnk'
Check (Test-Path $lnk) 'shortcut created on the given desktop'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
Check ($s.TargetPath -match 'wscript\.exe$' -and $s.Arguments -match 'open-panel\.vbs') 'shortcut runs open-panel.vbs via wscript'
Check (Test-Path (Join-Path $Repo 'data\guanjia.ico')) 'icon generated in data\'
$s.Arguments = '//B C:\owner\own-choice.vbs'
$s.Save()
$before = (Get-Item $lnk).LastWriteTimeUtc
$out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Install -Desktop $desk -Repo $Repo -Python $Py
$again = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
Check (($out -join ' ') -match 'left unchanged' -and $again.Arguments -eq '//B C:\owner\own-choice.vbs' -and (Get-Item $lnk).LastWriteTimeUtc -eq $before) 'existing shortcut is never overwritten'
Remove-Item -Recurse -Force $desk

'ALL PASS test_p21_panel_scripts'
