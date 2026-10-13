' GuanjiaASR-WSL (logon trigger): start the FireRedASR2 GPU recognition server inside WSL Ubuntu through wsl-asr-launcher.ps1, hidden
' (port 18798; model load takes ~20-60 s). Same structure as run-wsl-tts.vbs: the launcher RETRIES (up to 5 times, 30 s apart, until the
' port listens), logs every attempt to tmp\asr-autostart.log, and WAITS for the server so this task stays "Running" while it runs
' (MultipleInstancesPolicy=IgnoreNew relies on that). ASR is an OPTIONAL dependency of the voice daemon: if it is not up, the second-pass
' recognition falls back to SenseVoice on CPU. ASCII only: wscript reads this file with the system ANSI code page.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File F:\coding\guanjia\scripts\wsl-asr-launcher.ps1", 0, True)