# P2.1 面板快捷方式脚本用例：open-panel.ps1 的两条路（语音在跑 → 开网页；没跑 → 先拉起任务），install-panel-shortcut.ps1 生成快捷方式。
# P2.2 B5：显示名改「思绪」——新建三个「思绪」快捷方式，旧「管家」同目标的改名/删除，被主人改过目标的旧快捷方式不动。
# 只用 -DryRun 和临时桌面目录：不启动任务、不开浏览器、不碰真实桌面。
# 运行：powershell -NoProfile -ExecutionPolicy Bypass -File F:\coding\guanjia\tests\test_p21_panel_scripts.ps1
param([string]$Repo = 'F:\coding\guanjia', [string]$Py = (Join-Path $Repo '.venv\Scripts\python.exe'))
$ErrorActionPreference = 'Stop'
$Open = Join-Path $Repo 'scripts\open-panel.ps1'
$Install = Join-Path $Repo 'scripts\install-panel-shortcut.ps1'

function Check([bool]$Cond, [string]$What) {
    if ($Cond) { "PASS $What" } else { "FAIL $What"; exit 1 }
}
function NewDesk { $d = Join-Path $env:TEMP ('gj-desk-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Force $d | Out-Null; $d }
function RunInstall($d) { & powershell -NoProfile -ExecutionPolicy Bypass -File $Install -Desktop $d -Repo $Repo -Python $Py }

Check (Test-Path $Open) 'open-panel.ps1 exists'
$out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Open -Port 18993 -DryRun
Check (($out -join ' ') -match 'would start task GuanjiaVoice') 'voice not running -> start the task first'

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 18992)
$listener.Start()
try {
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Open -Port 18992 -Url 'http://127.0.0.1:18992/panel' -DryRun
    Check (($out -join ' ') -match 'would open http://127.0.0.1:18992/panel') 'voice running -> open the panel'
} finally { $listener.Stop() }

$sh = New-Object -ComObject WScript.Shell

# 1. 干净桌面：建三个「思绪」快捷方式
$desk = NewDesk
$null = RunInstall $desk
$lnk = Join-Path $desk '思绪面板.lnk'
Check (Test-Path $lnk) 'panel shortcut (思绪面板) created on the given desktop'
Check ((Test-Path (Join-Path $desk '思绪·开始语音.lnk')) -and (Test-Path (Join-Path $desk '思绪·停止语音.lnk'))) 'start / stop shortcuts created'
Check (-not (Test-Path (Join-Path $desk '管家面板.lnk'))) 'no old-name shortcut created'
$s = $sh.CreateShortcut($lnk)
Check ($s.TargetPath -match 'wscript\.exe$' -and $s.Arguments -match 'open-panel\.vbs') 'shortcut runs open-panel.vbs via wscript'
$st = $sh.CreateShortcut((Join-Path $desk '思绪·停止语音.lnk'))
Check ($st.TargetPath -match 'schtasks\.exe$' -and $st.Arguments -eq '/End /TN GuanjiaVoice') 'stop shortcut ends the GuanjiaVoice task (task name unchanged)'
Check (Test-Path (Join-Path $Repo 'data\guanjia.ico')) 'icon generated in data\'
$s.Arguments = '//B C:\owner\own-choice.vbs'
$s.Save()
$before = (Get-Item $lnk).LastWriteTimeUtc
$out = RunInstall $desk
$again = $sh.CreateShortcut($lnk)
Check (($out -join ' ') -match 'left unchanged' -and $again.Arguments -eq '//B C:\owner\own-choice.vbs' -and (Get-Item $lnk).LastWriteTimeUtc -eq $before) 'existing shortcut is never overwritten'
Remove-Item -Recurse -Force $desk

# 2. 旧桌面：三个旧「管家」快捷方式（同目标）→ 改名成「思绪」，旧名消失，目标不变
$desk = NewDesk
$wscript = 'C:\Windows\System32\wscript.exe'
$old = @(
    @{ Name = '管家面板'; Target = $wscript; Args = '//B ' + (Join-Path $Repo 'scripts\open-panel.vbs') },
    @{ Name = '管家·开始语音'; Target = 'C:\Windows\System32\schtasks.exe'; Args = '/Run /TN GuanjiaVoice' },
    @{ Name = '管家·停止语音'; Target = 'C:\Windows\System32\schtasks.exe'; Args = '/End /TN GuanjiaVoice' }
)
foreach ($o in $old) {
    $x = $sh.CreateShortcut((Join-Path $desk ($o.Name + '.lnk')))
    $x.TargetPath = $o.Target; $x.Arguments = $o.Args; $x.Save()
}
$null = RunInstall $desk
$names = (Get-ChildItem $desk -Filter *.lnk | ForEach-Object { $_.BaseName }) | Sort-Object
$want = (@('思绪·停止语音', '思绪·开始语音', '思绪面板') | Sort-Object) -join ','
Check (($names -join ',') -eq $want) 'old shortcuts renamed: only the three 思绪 shortcuts remain'
$moved = $sh.CreateShortcut((Join-Path $desk '思绪·开始语音.lnk'))
Check ($moved.TargetPath -match 'schtasks\.exe$' -and $moved.Arguments -eq '/Run /TN GuanjiaVoice') 'renamed shortcut keeps its target'
Remove-Item -Recurse -Force $desk

# 3. 旧快捷方式被主人改过目标：不动它；新的照建
$desk = NewDesk
$x = $sh.CreateShortcut((Join-Path $desk '管家面板.lnk'))
$x.TargetPath = $wscript; $x.Arguments = '//B C:\owner\own-choice.vbs'; $x.Save()
$null = RunInstall $desk
Check ((Test-Path (Join-Path $desk '管家面板.lnk')) -and (Test-Path (Join-Path $desk '思绪面板.lnk'))) 'owner-modified old shortcut kept, new one created'
Remove-Item -Recurse -Force $desk

# 4. 新旧并存且旧的同目标：删旧的
$desk = NewDesk
$null = RunInstall $desk
$x = $sh.CreateShortcut((Join-Path $desk '管家面板.lnk'))
$x.TargetPath = $wscript; $x.Arguments = '//B ' + (Join-Path $Repo 'scripts\open-panel.vbs'); $x.Save()
$out = RunInstall $desk
Check (-not (Test-Path (Join-Path $desk '管家面板.lnk')) -and (Test-Path (Join-Path $desk '思绪面板.lnk'))) 'new exists + old same target -> old removed'
Remove-Item -Recurse -Force $desk

'ALL PASS test_p21_panel_scripts'
