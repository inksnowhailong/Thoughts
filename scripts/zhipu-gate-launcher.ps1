# GuanjiaZhipuGate launcher: keep scripts\zhipu_gate.py (127.0.0.1:18793, Zhipu failover fuse) running; restart 3 s after any exit.
# ASCII only. Log: tmp\zhipu_gate_launcher.log (start/exit lines) and tmp\zhipu_gate.log (per-request lines, no secrets).
$ErrorActionPreference = 'Continue'
$py = 'F:\coding\guanjia\.venv\Scripts\python.exe'
$gate = 'F:\coding\guanjia\scripts\zhipu_gate.py'
$log = 'F:\coding\guanjia\tmp\zhipu_gate_launcher.log'
while ($true) {
    Add-Content -Path $log -Value ("{0} start" -f (Get-Date -Format s))
    & $py $gate
    Add-Content -Path $log -Value ("{0} exited code={1}, restart in 3s" -f (Get-Date -Format s), $LASTEXITCODE)
    Start-Sleep -Seconds 3
}
