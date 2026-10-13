' GuanjiaZhipuGate (logon trigger): run the Zhipu failover fuse (scripts\zhipu_gate.py, port 18793) hidden through its restart-loop launcher.
' The launcher never exits, so this task stays "Running" (MultipleInstancesPolicy=IgnoreNew relies on that). ASCII only.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File F:\coding\guanjia\scripts\zhipu-gate-launcher.ps1", 0, True)
