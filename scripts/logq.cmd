@echo off
rem Remote log query. Output goes to a UTF-8 file (tmp\logq-out.txt); the screen only gets one ASCII line,
rem so ssh/cmd GBK mangling never touches Chinese text. Usage: scripts\logq.cmd --recent 15
cd /d F:\coding\guanjia
.venv\Scripts\python.exe -m guanjia.logq %* --out tmp\logq-out.txt
