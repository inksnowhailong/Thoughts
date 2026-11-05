' agent-runtime（TS 大脑，18800）启动器：GuanjiaAgentRuntime 计划任务 → 本脚本 → 隐藏窗口跑 node
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "F:\coding\guanjia-v2-development\agent-runtime"
sh.Run """C:\Users\inksn\AppData\Local\Programs\nodejs\node.exe"" dist\src\index.js", 0, False
