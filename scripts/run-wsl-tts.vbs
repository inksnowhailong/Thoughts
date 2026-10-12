' GuanjiaTTS-WSL (logon trigger): start the TTS server inside WSL Ubuntu through wsl-tts-launcher.ps1, hidden (port 18791;
' the SCV voice is set in ~/guanjia-tts/env). The launcher RETRIES (up to 5 times, 30 s apart, until 18791 listens), logs
' every attempt to tmp\tts-autostart.log, and WAITS for the server so this task stays "Running" while TTS runs
' (MultipleInstancesPolicy=IgnoreNew relies on that). The WSL default distro on this PC is docker-desktop, so the launcher
' passes -d Ubuntu. ASCII only: wscript reads this file with the system ANSI code page.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File F:\coding\guanjia\scripts\wsl-tts-launcher.ps1", 0, True)