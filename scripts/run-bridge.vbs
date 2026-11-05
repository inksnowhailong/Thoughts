' guanjia bridge（Python 权限/工具/记忆桥，18801）启动器：GuanjiaBridge 计划任务 → 本脚本 → 隐藏窗口跑 python
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "F:\coding\guanjia-v2-development"
sh.Environment("PROCESS")("PYTHONPATH") = "F:\coding\guanjia-v2-development\src"
sh.Run """F:\coding\guanjia-v2-development\.venv\Scripts\python.exe"" -X utf8 -m guanjia.bridge_service", 0, False
