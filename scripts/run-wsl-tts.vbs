' GuanjiaTTS-WSL (logon trigger): start the TTS server inside WSL Ubuntu through wsl-tts-launcher.ps1, hidden (port 18791;
' the SCV voice is set in ~/guanjia-tts/env). The launcher is also a supervisor: it retries the start (30 s apart, until 18791
' listens), RESTARTS the server whenever it exits or the port stops answering (OOM kill, crash; every restart is logged to
' tmp\tts-autostart.log and recorded as a tts.* event), and waits for free VRAM before (re)starting while a game holds the GPU.
' It WAITS for the server, so this task stays "Running" while TTS runs (MultipleInstancesPolicy=IgnoreNew relies on that).
' The WSL default distro on this PC is docker-desktop, so the launcher passes -d Ubuntu.
' ASCII only: wscript reads this file with the system ANSI code page.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File F:\coding\guanjia\scripts\wsl-tts-launcher.ps1", 0, True)
