' GuanjiaVoice (logon trigger, interactive desktop session): start the voice supervisor hidden and WAIT for it,
' so the task stays "Running". When the task is ended (schtasks /End kills only wscript), the supervisor
' notices its parent is gone within 2 s and exits; its Job Object then ends python as well.
' ASCII only: WScript reads this file with the system ANSI code page.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File F:\coding\guanjia\scripts\voice-supervisor.ps1", 0, True)
