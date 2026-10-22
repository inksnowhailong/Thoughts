# Reports the modular runtime boundary. Read-only; does not start or stop services.
$Root = 'F:\coding\guanjia-v2-development'
$OldRoot = 'F:\coding\guanjia'
$DevPython = Join-Path $Root '.venv\Scripts\python.exe'
$Python = if (Test-Path $DevPython) { $DevPython } else { 'F:\python\python.exe' }
[pscustomobject]@{
    Root = $Root
    OldRootPreserved = (Test-Path $OldRoot)
    Python = $Python
    UsesProductionPython = ($Python -like "$OldRoot\*")
    VoiceSupervisor = Join-Path $Root 'scripts\voice-supervisor.ps1'
    VoiceLog = Join-Path $Root 'tmp\voice.log'
    SupervisorLog = Join-Path $Root 'tmp\voice-supervisor.log'
} | ConvertTo-Json -Depth 3
