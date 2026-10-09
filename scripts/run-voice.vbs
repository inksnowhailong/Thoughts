' GuanjiaVoice autostart (scheduled task, logon trigger). Window style 0 = hidden.
' Log appends to tmp\voice.log.
CreateObject("WScript.Shell").Run "cmd /c cd /d F:\coding\guanjia && F:\python\Scripts\uv.exe run python -m guanjia.voice >> tmp\voice.log 2>&1", 0, False
