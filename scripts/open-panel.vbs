' Desktop shortcut target (Guanjia panel): run open-panel.ps1 hidden. It starts the GuanjiaVoice task if the voice
' daemon is not running, then opens the panel in the default browser. ASCII only: WScript reads the system ANSI code page.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File F:\coding\guanjia\scripts\open-panel.ps1", 0, False
