' GuanjiaTTS-WSL (logon trigger): start IndexTTS-2.5 inside WSL Ubuntu, hidden (port 18791; the SCV voice is set
' in ~/guanjia-tts/env). WAIT for wsl.exe so the task stays "Running" while the TTS server runs.
' The WSL default distro on this PC is docker-desktop, so -d Ubuntu is required. ASCII only.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("wsl.exe -d Ubuntu -- bash /home/inksnow/guanjia-tts/run.sh", 0, True)
