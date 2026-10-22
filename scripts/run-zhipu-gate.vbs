' ASCII only: WScript reads this file with the system ANSI code page.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File F:\coding\guanjia-v2-development\scripts\zhipu-gate-launcher.ps1", 0, True)
