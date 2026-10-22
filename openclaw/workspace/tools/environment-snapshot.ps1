param(
    [string]$Sections = 'activity,tasks,weather,calendar,vision',
    [switch]$IncludeSystem
)

$ErrorActionPreference = 'Continue'
$Root = 'F:\coding\guanjia-v2-development'
$wanted = @($Sections.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
function Wants([string]$Name) { return $wanted -contains 'all' -or $wanted -contains $Name }

function Control-Status {
    try {
        return (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18795/status' -TimeoutSec 2).Content | ConvertFrom-Json
    } catch { return $null }
}

function Foreground-Status {
    try {
        $script = Join-Path $env:USERPROFILE '.openclaw\workspace\tools\get-desktop-status.ps1'
        return powershell -NoProfile -ExecutionPolicy Bypass -File $script | ConvertFrom-Json
    } catch { return @{ ok = $false; error = 'foreground_unavailable' } }
}

function Weather-Status {
    $cache = Join-Path $Root 'data\environment\weather.json'
    New-Item -ItemType Directory -Force -Path (Split-Path $cache) | Out-Null
    if (Test-Path $cache) {
        $age = ((Get-Date) - (Get-Item $cache).LastWriteTime).TotalMinutes
        if ($age -lt 15) { return Get-Content $cache -Raw -Encoding UTF8 | ConvertFrom-Json }
    }
    try {
        $url = 'https://api.open-meteo.com/v1/forecast?latitude=39.92&longitude=116.44&current=temperature_2m,apparent_temperature,weather_code,precipitation&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia%2FShanghai&forecast_days=1'
        $w = Invoke-RestMethod -Uri $url -TimeoutSec 3
        $value = [ordered]@{
            ok = $true
            place = '北京朝阳区'
            temperature_c = [math]::Round($w.current.temperature_2m)
            apparent_c = [math]::Round($w.current.apparent_temperature)
            precipitation_mm = $w.current.precipitation
            min_c = [math]::Round($w.daily.temperature_2m_min[0])
            max_c = [math]::Round($w.daily.temperature_2m_max[0])
            rain_probability = $w.daily.precipitation_probability_max[0]
            source = 'open-meteo'
            fetched_at = (Get-Date).ToString('o')
        }
        $value | ConvertTo-Json -Depth 4 | Set-Content $cache -Encoding UTF8
        return $value
    } catch {
        if (Test-Path $cache) {
            $stale = Get-Content $cache -Raw -Encoding UTF8 | ConvertFrom-Json
            $stale | Add-Member stale $true -Force
            return $stale
        }
        return @{ ok = $false; place = '北京朝阳区'; error = 'weather_unavailable' }
    }
}

function Calendar-Status {
    $today = Get-Date
    $names = New-Object System.Collections.Generic.List[string]
    $fixed = @{
        '01-01' = '元旦'; '02-14' = '情人节'; '03-08' = '妇女节'; '05-01' = '劳动节'
        '06-01' = '儿童节'; '10-01' = '国庆节'; '12-25' = '圣诞节'
    }
    $md = $today.ToString('MM-dd')
    if ($fixed.ContainsKey($md)) { $names.Add($fixed[$md]) }
    try {
        $calendar = [System.Globalization.ChineseLunisolarCalendar]::new()
        $lunarMonth = $calendar.GetMonth($today)
        $leap = $calendar.GetLeapMonth($calendar.GetYear($today))
        if ($leap -gt 0 -and $lunarMonth -ge $leap) { $lunarMonth-- }
        $lunarDay = $calendar.GetDayOfMonth($today)
        $lunarFestivals = @{
            '1-1' = '春节'; '1-15' = '元宵节'; '5-5' = '端午节'
            '7-7' = '七夕'; '8-15' = '中秋节'; '9-9' = '重阳节'; '12-8' = '腊八节'
        }
        $lunarKey = "$lunarMonth-$lunarDay"
        if ($lunarFestivals.ContainsKey($lunarKey)) { $names.Add($lunarFestivals[$lunarKey]) }
    } catch {}
    return @{
        date = $today.ToString('yyyy-MM-dd')
        weekday = $today.ToString('dddd')
        festivals = @($names)
        special = $names.Count -gt 0
    }
}

function System-Status {
    $os = Get-CimInstance Win32_OperatingSystem
    $gpu = $null
    try {
        $parts = ((& nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1) -split ',') | ForEach-Object { $_.Trim() }
        if ($parts.Count -eq 3) {
            $gpu = @{ utilization_percent = [int]$parts[0]; memory_used_mb = [int]$parts[1]; memory_total_mb = [int]$parts[2] }
        }
    } catch {}
    return @{
        memory_free_mb = [math]::Round($os.FreePhysicalMemory / 1024)
        memory_total_mb = [math]::Round($os.TotalVisibleMemorySize / 1024)
        f_drive_free_gb = [math]::Round((Get-PSDrive F).Free / 1GB, 1)
        gpu = $gpu
    }
}

$status = Control-Status
$result = [ordered]@{
    ok = $true
    captured_at = (Get-Date).ToString('o')
    time = (Get-Date).ToString('yyyy-MM-dd HH:mm dddd')
}

if (Wants 'activity') {
    $result.activity = [ordered]@{
        foreground = Foreground-Status
        gaming = [bool]$status.gaming
        watching_media = [bool]($status.program.media_candidate -or $status.program.quiet_active)
        runtime = $status.runtime
        muted = [bool]$status.muted
    }
}
if (Wants 'tasks') {
    $result.tasks = if ($status -and $status.tasks) {
        @{
            counts = $status.tasks.counts
            active = @($status.tasks.active_tasks)
            recent = @($status.tasks.recent_tasks | Select-Object -First 5)
        }
    } else { @{ ok = $false; error = 'tasks_unavailable' } }
}
if (Wants 'weather') { $result.weather = Weather-Status }
if (Wants 'calendar') { $result.calendar = Calendar-Status }
if (Wants 'vision') {
    $vision = Join-Path $Root 'data\vision\latest.json'
    $result.vision = if (Test-Path $vision) {
        Get-Content $vision -Raw -Encoding UTF8 | ConvertFrom-Json
    } else { @{ ok = $false; reason = 'no_vision_frame_yet' } }
}
if ($IncludeSystem -or (Wants 'system')) { $result.system = System-Status }
if (Wants 'services') {
    $result.services = @{
        gateway = [bool]$status.gateway
        tts = [bool]$status.tts
        state = $status.state
    }
}

$result | ConvertTo-Json -Depth 8 -Compress
