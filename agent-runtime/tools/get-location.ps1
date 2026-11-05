<#
.SYNOPSIS
  Read-only location probe for weather lookups (guanjia L0 tool).
.DESCRIPTION
  Asks the Windows location service (Wi-Fi / GNSS / cellular / user default location) for the
  current position. IP-based fixes are rejected on purpose: this PC usually runs a VPN, so an
  IP lookup would report the VPN exit. Any failure falls back to Beijing.
  The script writes no files and makes no network calls of its own.
  Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less files with the ANSI code page.
  (Migrated verbatim from openclaw/workspace/tools/get-location.ps1; the bridge's
  get_location/get_weather tools invoke this script the same way the old guard plugin did.)
.OUTPUTS
  One line of JSON: {lat, lon, source, accuracyM, positionSource, reason}
  source is "windows-location" or "fallback-beijing"; reason says why a fallback happened.
#>
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$BeijingLat = 39.9042
$BeijingLon = 116.4074
# Wi-Fi fixes are usually tens to hundreds of meters; coarser than this is not "the home"
$MaxAccuracyM = 5000
$AccessWaitMs = 2000
$FixTimeoutSec = 5
# Only these sources are independent of the network exit (VPN)
$TrustedSources = @('WiFi', 'Satellite', 'Cellular', 'Default')

function Out-Location($lat, $lon, $source, $accuracy, $positionSource, $reason) {
    # Two decimals (~1 km) is enough for weather and keeps the exact home position out of the cloud
    [ordered]@{
        lat            = [Math]::Round([double]$lat, 2)
        lon            = [Math]::Round([double]$lon, 2)
        source         = $source
        accuracyM      = $accuracy
        positionSource = $positionSource
        reason         = $reason
    } | ConvertTo-Json -Compress
    exit 0
}

function Out-Fallback($reason, $accuracy = $null, $positionSource = $null) {
    Out-Location $BeijingLat $BeijingLon 'fallback-beijing' $accuracy $positionSource $reason
}

function Get-Reason($err) {
    while ($err.InnerException) { $err = $err.InnerException }
    if ($err -is [UnauthorizedAccessException]) { return 'access-denied' }
    # 0x800705B4 = ERROR_TIMEOUT from GetGeopositionAsync
    if ($err.Message -eq 'timeout' -or $err.HResult -eq -2147023436) { return 'timeout' }
    return 'error-' + $err.GetType().Name
}

function Wait-WinRt($operation, [Type]$resultType, [int]$timeoutMs) {
    $task = $script:AsTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait($timeoutMs)) { throw 'timeout' }
    return $task.Result
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $script:AsTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $null = [Windows.Devices.Geolocation.Geolocator, Windows.Devices.Geolocation, ContentType = WindowsRuntime]
} catch {
    Out-Fallback 'winrt-unavailable'
}

$locator = $null
try {
    $access = Wait-WinRt ([Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()) ([Windows.Devices.Geolocation.GeolocationAccessStatus]) $AccessWaitMs
    if ("$access" -ne 'Allowed') { Out-Fallback "access-$access" }
    $locator = New-Object Windows.Devices.Geolocation.Geolocator
    $locator.DesiredAccuracyInMeters = 100
    $op = $locator.GetGeopositionAsync([TimeSpan]::FromMinutes(10), [TimeSpan]::FromSeconds($FixTimeoutSec))
    $pos = Wait-WinRt $op ([Windows.Devices.Geolocation.Geoposition]) (($FixTimeoutSec + 1) * 1000)
} catch {
    $reason = Get-Reason $_.Exception
    # Disabled = location switched off in Settings; NoData = no Wi-Fi/GNSS signal to locate with
    if ($locator -and "$($locator.LocationStatus)" -in @('Disabled', 'NoData', 'NotAvailable')) { $reason = "$reason-status-$($locator.LocationStatus)" }
    Out-Fallback $reason
}

$coord = $pos.Coordinate
$src = "$($coord.PositionSource)"
$acc = [int][Math]::Round($coord.Accuracy)
if ($TrustedSources -notcontains $src) { Out-Fallback "untrusted-source-$src" $acc $src }
if ($acc -gt $MaxAccuracyM) { Out-Fallback 'inaccurate' $acc $src }
Out-Location $coord.Point.Position.Latitude $coord.Point.Position.Longitude 'windows-location' $acc $src 'ok'
