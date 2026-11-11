# Token Burn Gauge by soybean_alien
# Stops the app and the collectors that run from this script's own folder (used by the uninstaller).
$here = $PSScriptRoot
Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -like 'python*' -or $_.Name -eq 'TokenGauge.exe') -and $_.CommandLine -like "*$here*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
